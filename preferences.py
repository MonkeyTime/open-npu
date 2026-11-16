"""Préférences locales de l’agent, séparées de son identité réseau."""

import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Event, Lock

from network import MAX_RATE, MIN_UPLOAD_LIMIT, optional_number


def save_json(path, values):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(values, output)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Preferences:
    def __init__(self, config_path):
        self.path = config_path.with_suffix(".preferences.json")
        self.changed = Event()
        self.lock = Lock()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            default = optional_number(config.get("upload_limit_mbps"), MIN_UPLOAD_LIMIT, MAX_RATE)
        except (OSError, AttributeError, ValueError):
            default = None
        self.values = {"paused": False, "upload_limit_mbps": default}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(saved, dict) or type(saved.get("paused", False)) is not bool:
                raise ValueError("Préférences invalides.")
            self.values = {"paused": saved.get("paused", False), "upload_limit_mbps":
                           optional_number(saved.get("upload_limit_mbps", default), MIN_UPLOAD_LIMIT, MAX_RATE)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logging.exception("Préférences illisibles. Le partage reste en pause.")
            self.values["paused"] = True

    def snapshot(self):
        with self.lock:
            return self.values["paused"], self.values["upload_limit_mbps"]

    def update(self, **fields):
        with self.lock:
            values = self.values | fields
            save_json(self.path, values)
            self.values = values
            self.changed.set()
