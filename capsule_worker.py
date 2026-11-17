"""Exécute un étage signé sur NPU et conserve un seul graphe compilé à la fois."""

import hashlib
import json
import logging
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

import numpy as np
from openvino import Core

from capsule_manifest import HEX, tensor_size, verify_manifest

EXECUTION_TIMEOUT = 90
NUMPY_TYPES = {"f16": "<f2", "f32": "<f4", "i32": "<i4"}


def execute_stages(connection, directory):
    compiled = None
    try:
        while True:
            stage = json.loads(connection.recv_bytes(4096))
            raw = (Path(directory) / "input.bin").read_bytes()
            if "model" in stage:
                core = Core()
                xml = Path(stage["model"])
                model = core.read_model(xml, xml.with_suffix(".bin"))
                if len(model.inputs) != 1 or len(model.outputs) != 1:
                    raise ValueError("Une étape exige exactement une entrée et une sortie.")
                for port, spec in ((model.input(0), stage["input"]), (model.output(0), stage["output"])):
                    if list(port.shape) != spec["shape"] or port.get_element_type().get_type_name() != spec["dtype"]:
                        raise ValueError("Le graphe ne respecte pas son contrat signé.")
                compiled = core.compile_model(model, "NPU")
            values = np.frombuffer(raw, dtype=NUMPY_TYPES[stage["input"]["dtype"]]).reshape(stage["input"]["shape"])
            result = compiled([values])[compiled.output(0)]
            (Path(directory) / "output.bin").write_bytes(result.astype(
                NUMPY_TYPES[stage["output"]["dtype"]], copy=False).tobytes(order="C"))
            connection.send_bytes(b"1")
    except (EOFError, OSError, RuntimeError, ValueError):
        pass
    finally:
        connection.close()


class CapsuleWorker:
    def __init__(self, public_key_file, post, open_request):
        self.public_key = public_key_file.read_text(encoding="ascii").strip()
        if not HEX.fullmatch(self.public_key):
            raise ValueError("Clé publique des capsules invalide.")
        self.fingerprint = hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest()
        self.post = post
        self.open_request = open_request
        self.process = None
        self.connection = None
        self.scratch = None
        self.cache_key = None
        self.task = None
        self.output = None

    def exchange(self, endpoint, body=None, *, limit=None):
        task = self.task
        path = f"/v2/node/stages/{task['job']}/{endpoint}"
        return self.open_request(path, body, content_type="application/octet-stream",
                                 headers={"X-Stage-Lease": task["lease"]}, limit=limit)

    def download(self, path, extension, expected):
        total = 0
        sha256 = hashlib.sha256()
        with self.exchange(f"assets/{extension}") as response, path.open("wb") as output:
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > expected["size"]:
                    raise ValueError("Artefact plus grand que son manifeste.")
                sha256.update(chunk)
                output.write(chunk)
        if total != expected["size"] or sha256.hexdigest() != expected["sha256"]:
            raise ValueError("Artefact altéré.")

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join()
            self.process.close()
            self.process = None
        if self.scratch is not None:
            self.scratch.cleanup()
            self.scratch = None
        self.cache_key = None

    def infer(self, stage, raw):
        if self.process is None:
            context = get_context("spawn")
            parent, child = context.Pipe()
            self.scratch = TemporaryDirectory(prefix="open-npu-inference-")
            process = context.Process(target=execute_stages, args=(child, self.scratch.name), daemon=True)
            try:
                process.start()
            except BaseException:
                parent.close()
                self.close()
                raise
            finally:
                child.close()
            self.process, self.connection = process, parent
        expected = tensor_size(stage["output"])
        try:
            (Path(self.scratch.name) / "input.bin").write_bytes(raw)
            self.connection.send_bytes(json.dumps(stage).encode())
            if not self.connection.poll(EXECUTION_TIMEOUT):
                raise TimeoutError("Délai du processus NPU dépassé.")
            if self.connection.recv_bytes(1) != b"1":
                raise ValueError("Réponse du processus NPU illisible.")
            with (Path(self.scratch.name) / "output.bin").open("rb") as output:
                result = output.read(expected + 1)
            if len(result) != expected:
                raise ValueError("Réponse du processus NPU incomplète.")
            return result
        except (OSError, EOFError, ValueError) as error:
            self.close()
            raise RuntimeError("Le processus NPU a échoué.") from error

    def evaluate(self):
        task = self.task
        manifest, identity = verify_manifest(task["capsule"], self.public_key)
        if identity != task["digest"]:
            raise ValueError("Empreinte de capsule incohérente.")
        stage = manifest["stages"][task["stage"]]
        cache_key = (identity, task["stage"])
        expected = tensor_size(stage["input"])
        with self.exchange("input") as response:
            raw = response.read(expected + 1)
        if len(raw) != expected:
            raise ValueError("Tenseur d’entrée incomplet.")
        contract = {key: stage[key] for key in ("input", "output")}
        if cache_key == self.cache_key:
            return self.infer(contract, raw)
        self.close()
        with TemporaryDirectory(prefix="open-npu-capsule-") as directory:
            xml = Path(directory) / "stage.xml"
            self.download(xml, "xml", stage["xml"])
            self.download(xml.with_suffix(".bin"), "bin", stage["bin"])
            result = self.infer(contract | {"model": str(xml)}, raw)
        self.cache_key = cache_key
        logging.info("Capsule %s v%s étape %d compilée sur NPU dans un processus dédié.",
                     manifest["id"], manifest["version"], task["stage"] + 1)
        return result

    def poll(self, limit, status):
        try:
            if self.task is None:
                self.task = self.post("/v2/node/claim", {"fingerprint": self.fingerprint})["task"]
            if self.task is None:
                return False
            status("Capsule · calcul par étapes", "working")
            try:
                if self.output is None:
                    self.output = self.evaluate()
            except (ValueError, RuntimeError, KeyError, IndexError):
                logging.exception("Étape OpenVINO non exécutable.")
                with self.exchange("fail", b""):
                    pass
            else:
                try:
                    with self.exchange("result", self.output, limit=limit):
                        pass
                except HTTPError as error:
                    if error.code != 400:
                        raise
                    with self.exchange("fail", b""):
                        pass
            self.task = self.output = None
            return True
        except HTTPError as error:
            if error.code in (400, 401, 403, 404, 409):
                self.task = self.output = None
            if error.code not in (404, 409):
                logging.info("Capsule indisponible (HTTP %d).", error.code)
            return False
        except OSError as error:
            logging.info("Envoi de capsule différé (%s).", type(error).__name__)
            return False
