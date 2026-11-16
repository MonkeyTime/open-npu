"""Préférences locales de l’agent, séparées de son identité réseau."""

import json
import logging
import os
from pathlib import Path
import re
import tempfile
from threading import Event, Lock

from network import MAX_RATE, MIN_UPLOAD_LIMIT, optional_number


def clock_minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Utilisez une heure au format HH:MM.")
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def pause_reason(settings, now, plugged_in):
    if settings["paused"]:
        return "Partage en pause"
    if settings["ac_only"] and not plugged_in:
        return "En attente du secteur"
    if settings["schedule_start"] is not None:
        start = clock_minutes(settings["schedule_start"])
        end = clock_minutes(settings["schedule_end"])
        within = start <= now < end if start < end else now >= start or now < end
        if not within:
            return "Hors plage horaire"
    return None


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
        self.values = {"paused": False, "upload_limit_mbps": default, "ac_only": False,
                       "schedule_start": None, "schedule_end": None}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(saved, dict) or any(type(saved.get(key, False)) is not bool for key in ("paused", "ac_only")):
                raise ValueError("Préférences invalides.")
            start = saved.get("schedule_start")
            end = saved.get("schedule_end")
            if start is not None or end is not None:
                if clock_minutes(start) == clock_minutes(end):
                    raise ValueError("Heures de contribution identiques.")
            self.values.update(paused=saved.get("paused", False), ac_only=saved.get("ac_only", False),
                               schedule_start=start, schedule_end=end, upload_limit_mbps=
                               optional_number(saved.get("upload_limit_mbps", default), MIN_UPLOAD_LIMIT, MAX_RATE))
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

    def settings(self):
        with self.lock:
            return self.values.copy()
