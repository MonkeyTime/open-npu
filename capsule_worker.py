"""Exécute un étage signé sur NPU et conserve un seul graphe compilé à la fois."""

from contextlib import ExitStack
import hashlib
import json
import logging
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

import numpy as np
from openvino import Core

from capsule_manifest import CAPSULE_ASSETS, HEX, tensor_size, verify_manifest
from npu import AMD_PROVIDER, amd_npu, amd_session, onnx_bytes

EXECUTION_TIMEOUT = 90
NUMPY_TYPES = {"f16": "<f2", "f32": "<f4", "i32": "<i4"}


def compile_stage(path, stage, target, accelerator):
    specs = [stage["input"], stage["output"]]
    if target == AMD_PROVIDER:
        session = amd_session(accelerator, onnx_bytes(path))
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("Une étape exige exactement une entrée et une sortie.")
        types = {"f16": "tensor(float16)", "f32": "tensor(float)", "i32": "tensor(int32)"}
        for port, spec in zip((inputs[0], outputs[0]), specs):
            if port.shape != spec["shape"] or port.type != types[spec["dtype"]]:
                raise ValueError("Le graphe ne respecte pas son contrat signé.")
        return lambda values: session.run([outputs[0].name], {inputs[0].name: values})[0]

    core = Core()
    if path.suffix == ".onnx":
        model = core.read_model(onnx_bytes(path), b"")
    else:
        model = core.read_model(path, path.with_suffix(".bin"))
    if len(model.inputs) != 1 or len(model.outputs) != 1:
        raise ValueError("Une étape exige exactement une entrée et une sortie.")
    for port, spec in zip((model.input(0), model.output(0)), specs):
        if list(port.shape) != spec["shape"] or port.get_element_type().get_type_name() != spec["dtype"]:
            raise ValueError("Le graphe ne respecte pas son contrat signé.")
    model = core.compile_model(model, target)
    return lambda values: model([values])[model.output(0)]


def execute_stages(connection, directory, target):
    try:
        with ExitStack() as resources:
            accelerator = resources.enter_context(amd_npu()) if target == AMD_PROVIDER else None
            compiled = None
            try:
                while True:
                    stage = json.loads(connection.recv_bytes(4096))
                    raw = (Path(directory) / "input.bin").read_bytes()
                    if "model" in stage:
                        compiled = compile_stage(Path(stage["model"]), stage, target, accelerator)
                    values = np.frombuffer(raw, dtype=NUMPY_TYPES[stage["input"]["dtype"]]).reshape(stage["input"]["shape"])
                    result = compiled(values)
                    (Path(directory) / "output.bin").write_bytes(result.astype(
                        NUMPY_TYPES[stage["output"]["dtype"]], copy=False).tobytes(order="C"))
                    connection.send_bytes(b"1")
            finally:
                del compiled
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            connection.send_bytes(b"0" + str(error).encode("utf-8")[:4095])
        except OSError:
            pass
    finally:
        connection.close()


class CapsuleWorker:
    def __init__(self, public_key_file, post, open_request, target):
        self.public_key = public_key_file.read_text(encoding="ascii").strip()
        if not HEX.fullmatch(self.public_key):
            raise ValueError("Clé publique des capsules invalide.")
        self.fingerprint = hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest()
        self.post = post
        self.open_request = open_request
        self.target = target
        self.formats = [2] if target == AMD_PROVIDER else [1, 2]
        self.claim_path = "/v2/node/claim-onnx" if target == AMD_PROVIDER else "/v2/node/claim"
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
            process = context.Process(target=execute_stages, args=(child, self.scratch.name, self.target), daemon=True)
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
            response = self.connection.recv_bytes(4096)
            if response.startswith(b"0"):
                raise ValueError(response[1:].decode("utf-8", errors="replace"))
            if response != b"1":
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
        if manifest["format"] not in self.formats:
            raise ValueError("Format de capsule incompatible avec cette NPU.")
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
            extensions = CAPSULE_ASSETS[manifest["format"]]
            for extension in extensions:
                self.download(Path(directory) / f"stage.{extension}", extension, stage[extension])
            result = self.infer(contract | {"model": str(Path(directory) / f"stage.{extensions[0]}")}, raw)
        self.cache_key = cache_key
        logging.info("Capsule %s v%s étape %d compilée sur NPU dans un processus dédié.",
                     manifest["id"], manifest["version"], task["stage"] + 1)
        return result

    def poll(self, limit, status):
        try:
            if self.task is None:
                self.task = self.post(self.claim_path,
                                      {"fingerprint": self.fingerprint, "formats": self.formats})["task"]
            if self.task is None:
                return False
            status("Capsule · calcul par étapes", "working")
            try:
                if self.output is None:
                    self.output = self.evaluate()
            except (ValueError, RuntimeError, KeyError, IndexError):
                logging.exception("Étape non exécutable sur la NPU.")
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
