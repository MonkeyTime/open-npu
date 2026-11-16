"""Agent Windows OPEN-NPU. Le seul graphe exécutable est intégré au binaire."""

import argparse
import ctypes
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import random
import secrets
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np
from openvino import Core, Model, Type, opset13

from capsule import CAPSULE, WIDTH, valid_values
from network import (MAX_RATE, MIN_UPLOAD_LIMIT, PROBE_BYTES, PROBE_INTERVAL, estimate_mbps,
                     optional_number, paced_body)
from preferences import Preferences, pause_reason, save_json
from tray import run_tray


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect)
AGENT_VERSION = "0.2.0"


class SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("battery", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                ("flags", ctypes.c_ubyte), ("remaining", ctypes.c_uint32), ("full", ctypes.c_uint32)]


def contribution(preferences):
    settings = preferences.settings()
    plugged_in = True
    if settings["ac_only"]:
        power = SystemPowerStatus()
        plugged_in = bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))) and power.ac == 1
    now = datetime.now()
    return pause_reason(settings, now.hour * 60 + now.minute, plugged_in), settings["upload_limit_mbps"]


def compile_capsule():
    core = Core()
    if "NPU" not in core.available_devices:
        raise RuntimeError(f"NPU indisponible dans OpenVINO. Périphériques vus : {core.available_devices}")
    values = opset13.parameter([1, WIDTH], Type.f16, name="values")
    model = Model([opset13.multiply(values, values)], [values], CAPSULE)
    device = core.get_property("NPU", "FULL_DEVICE_NAME")
    properties = core.get_property("NPU", "SUPPORTED_PROPERTIES")
    hardware = None
    if "DEVICE_ARCHITECTURE" in properties and "NPU_MAX_TILES" in properties:
        architecture = core.get_property("NPU", "DEVICE_ARCHITECTURE")
        tiles = core.get_property("NPU", "NPU_MAX_TILES")
        hardware = f"{device} · OpenVINO {architecture} · {tiles} tuiles"
    return core.compile_model(model, "NPU"), device, hardware


def calculate(compiled, values):
    if not valid_values(values, WIDTH):
        raise ValueError("Entrée hors contrat de la capsule.")
    tensor = np.zeros((1, WIDTH), dtype=np.float16)
    tensor[0, :len(values)] = values
    return compiled([tensor])[compiled.output(0)][0, :len(values)].astype(float).tolist()


def request_bytes(config, route, body=None, *, credential="token", content_type=None, method=None, limit=None):
    headers = {"Authorization": f"Bearer {config[credential]}"}
    if body is not None:
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        if limit is not None:
            body = paced_body(body, limit)
    request = Request(
        config["server"].rstrip("/") + route,
        data=body, headers=headers, method=method,
    )
    with HTTP.open(request, timeout=15) as response:
        return response.read()


def api(config, route, data, *, credential="token", limit=None):
    return json.loads(request_bytes(config, route, json.dumps(data).encode(), credential=credential,
                                    content_type="application/json", limit=limit))


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    server = urlsplit(config["server"])
    local_http = server.scheme == "http" and server.hostname in ("127.0.0.1", "::1", "localhost")
    if not server.hostname or server.username or server.password or server.query or server.fragment:
        raise ValueError("Adresse du serveur invalide.")
    if not local_http and server.scheme != "https":
        raise ValueError("HTTPS requis hors de la boucle locale.")
    for key in ("token", "enrollment_key"):
        if key in config and (not isinstance(config[key], str) or not config[key]):
            raise ValueError(f"Configuration invalide ({key}).")
    if not config.get("token") and not config.get("enrollment_key"):
        raise ValueError("Identité et clé d’inscription manquantes.")
    optional_number(config.get("upload_limit_mbps"), MIN_UPLOAD_LIMIT, MAX_RATE)
    return config


def enroll(config, path, stop):
    if "enrollment_key" not in config:
        return
    if "token" not in config:
        config["token"] = secrets.token_urlsafe(32)
        save_json(path, config)
    identity = {"name": (socket.gethostname().strip() or "Poste Windows")[:100], "identity": config["token"]}
    while not stop.is_set():
        try:
            result = api(config, "/v1/node/enroll", identity, credential="enrollment_key")
            break
        except HTTPError as error:
            if 400 <= error.code < 500 and error.code not in (408, 429):
                raise
            logging.warning("Inscription différée (HTTP %d).", error.code)
        except (OSError, ValueError) as error:
            logging.warning("Inscription différée (%s).", type(error).__name__)
        stop.wait(15)
    else:
        return
    del config["enrollment_key"]
    save_json(path, config)
    logging.info("Nœud inscrit automatiquement (#%s).", result["id"])


def measure_network(config):
    path = "/v1/node/network-probe"
    started = time.monotonic()
    request_bytes(config, path, method="HEAD")
    rtt = time.monotonic() - started
    local = urlsplit(config["server"]).hostname in ("127.0.0.1", "::1", "localhost")
    sample = {"rtt_ms": max(0.01, round(rtt * 1000, 2)), "local_link": local}
    if local:
        return sample
    outgoing = os.urandom(PROBE_BYTES)
    started = time.monotonic()
    request_bytes(config, path, outgoing, content_type="application/octet-stream", limit=config.get("upload_limit_mbps"))
    sample["upload_mbps"] = estimate_mbps(PROBE_BYTES, time.monotonic() - started, rtt)
    started = time.monotonic()
    received = request_bytes(config, path)
    if len(received) != PROBE_BYTES:
        raise ValueError("Sonde descendante incomplète.")
    sample["download_mbps"] = estimate_mbps(PROBE_BYTES, time.monotonic() - started, rtt)
    return sample


def run(config, compiled, device, hardware, stop, status, preferences):
    pending = None
    network = {}
    capacity = {"capsule": CAPSULE, "device": device, "network": network, "agent_version": AGENT_VERSION}
    probe_after = 0
    sample_limit = None
    previous_limit = None
    idle_delay = 2
    if hardware:
        capacity["hardware"] = hardware
    while not stop.is_set() or pending is not None:
        preferences.changed.clear()
        reason, limit = contribution(preferences)
        if limit != previous_limit:
            probe_after = 0
            previous_limit = limit
        delay = 2
        try:
            if pending is not None:
                api(config, "/v1/node/result", pending, limit=limit)
                logging.info("Résultat envoyé pour %s, lot %d.", pending["job"], pending["position"])
                pending = None
            if stop.is_set():
                break
            if not reason and time.monotonic() >= probe_after:
                try:
                    network["sample"] = measure_network(config | {"upload_limit_mbps": limit})
                    sample_limit = limit
                    probe_after = time.monotonic() + PROBE_INTERVAL
                except HTTPError as error:
                    probe_after = time.monotonic() + (PROBE_INTERVAL if error.code in (404, 405, 429) else 60)
                    logging.info("Mesure réseau différée (HTTP %d).", error.code)
                except (OSError, ValueError) as error:
                    probe_after = time.monotonic() + 60
                    logging.info("Mesure réseau différée (%s).", type(error).__name__)
            if stop.is_set():
                break
            reason, limit = contribution(preferences)
            capacity["paused"] = bool(reason)
            network["upload_limit_mbps"] = limit
            if limit != sample_limit:
                network.pop("sample", None)
            task = api(config, "/v1/node/claim", capacity)["task"]
            network.pop("sample", None)
            if task:
                idle_delay = 2
                status("Calcul en cours", "working")
                if task["capsule"] != CAPSULE:
                    raise ValueError("Capsule inconnue.")
                pending = {key: task[key] for key in ("job", "position", "lease")}
                pending["values"] = calculate(compiled, task["values"])
                continue
            status(reason or "Disponible", "waiting" if reason else "ready")
            delay = idle_delay
            idle_delay = min(12, idle_delay * 1.5)
        except HTTPError as error:
            logging.warning("Réponse HTTP %d.", error.code)
            if error.code == 401:
                raise RuntimeError("Jeton du nœud refusé.") from error
            if error.code == 403:
                pending = None
                status("Désactivé par l’administration", "waiting")
            else:
                status("Serveur indisponible", "waiting")
            if error.code in (400, 409, 413, 415):
                pending = None
            delay = 10 if error.code == 403 else 5
        except (OSError, URLError) as error:
            logging.warning("Connexion ou réponse interrompue (%s).", type(error).__name__)
            status("En attente du serveur", "waiting")
            delay = 5
        except (RuntimeError, ValueError, KeyError):
            logging.exception("Échec du traitement du lot.")
            pending = None
            status("Erreur de calcul", "error")
            delay = 5
        preferences.changed.wait(delay + random.uniform(0, delay * 0.15))
        if stop.is_set():
            break


def run_agent(path, stop, status, preferences):
    try:
        config = load_config(path)
        if config.get("enrollment_key"):
            status("Inscription au réseau", "waiting")
            enroll(config, path, stop)
        if not stop.is_set():
            status("Initialisation de la NPU", "waiting")
            compiled, device, hardware = probe_capsule()
            run(config, compiled, device, hardware, stop, status, preferences)
    except Exception:
        logging.exception("Arrêt du calcul OPEN-NPU.")
        status("Erreur · consulter le journal", "error")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("runtime/node.json"))
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    log_file = args.log or args.config.with_name("agent.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")],
    )
    try:
        if args.probe:
            probe_capsule()
        else:
            with args.config.with_suffix(".lock").open("a+b") as lock:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    logging.info("Un agent utilise déjà cette configuration.")
                    return 0
                preferences = Preferences(args.config)
                run_tray(log_file, preferences, lambda stop, status: run_agent(args.config, stop, status, preferences),
                         version=AGENT_VERSION)
    except Exception:
        logging.exception("Arrêt de l'agent.")
        return 1
    return 0


def probe_capsule():
    compiled, device, hardware = compile_capsule()
    check = calculate(compiled, [2, -3, 4])
    if check != [4, 9, 16]:
        raise RuntimeError(f"L'autotest NPU a produit {check}.")
    logging.info("Autotest réussi sur %s, périphériques d'exécution %s.", device,
                 compiled.get_property("EXECUTION_DEVICES"))
    return compiled, device, hardware


if __name__ == "__main__":
    raise SystemExit(main())
