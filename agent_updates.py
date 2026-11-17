"""Download Windows releases and install them only after confirmation."""

import hashlib
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from preferences import save_json

CHECK_INTERVAL = 24 * 60 * 60
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXTRACTED = 1024 * 1024 * 1024
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}")
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
TOP_LEVEL = {"OpenNpuAgent", "install-agent.ps1", "release.json", "verify-agent-package.ps1",
             "package-manifest.json", "package-manifest.p7s"}


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def version_tuple(text):
    if not isinstance(text, str) or not VERSION.fullmatch(text):
        raise ValueError("Version de mise à jour invalide.")
    return tuple(map(int, text.split(".")))


def validate_release(data):
    if not isinstance(data, dict):
        raise ValueError("Métadonnées de mise à jour invalides.")
    version = data.get("version")
    version_tuple(version)
    expected = f"/downloads/agent/OpenNpuAgent-{version}-windows-x64.zip"
    if (data.get("url") != expected or type(data.get("signed")) is not bool
            or not isinstance(data.get("sha256"), str) or not SHA256.fullmatch(data["sha256"])
            or type(data.get("size")) is not int or not 0 < data["size"] <= MAX_ARCHIVE):
        raise ValueError("Métadonnées de mise à jour invalides.")
    return {key: data[key] for key in ("version", "url", "signed", "sha256", "size")}


def unpack_release(archive, destination, *, enrollment=False):
    allowed = TOP_LEVEL | {"enrollment.json"} if enrollment else TOP_LEVEL
    seen = set()
    with ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 8192 or sum(info.file_size for info in entries) > MAX_EXTRACTED:
            raise ValueError("Archive trop volumineuse.")
        for info in entries:
            parts = info.filename.removesuffix("/").split("/")
            key = "/".join(parts).casefold()
            if (key in seen or parts[0] not in allowed or (len(parts) > 1 and parts[0] != "OpenNpuAgent")
                    or stat.S_ISLNK(info.external_attr >> 16) or any(
                        not part or part.endswith((".", " ")) or re.search(r'[\\:\x00-\x1f<>"|?*]', part)
                        or PureWindowsPath(part).is_reserved() for part in parts)):
                raise ValueError("Chemin non autorisé dans le paquet.")
            seen.add(key)
            path = destination.joinpath(*parts)
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)


class AgentUpdates:
    def __init__(self, config, version, load_config, opener, *, installed=None):
        self.config = config.resolve()
        self.install = self.config.parent
        self.version = version_tuple(version)
        self.load_config = load_config
        self.open = opener
        self.directory = self.install / "updates"
        self.state_file = self.directory / "status.json"
        self.archive = self.directory / "pending.zip"
        executable = self.install / "agent" / "OpenNpuAgent.exe"
        self.installed = (self.config.name == "node.json" and getattr(sys, "frozen", False)
                          and executable.resolve() == Path(sys.executable).resolve()) if installed is None else installed
        self.state = read_object(self.state_file)
        self.lock = Lock()

    def connection(self):
        return self.load_config()["server"].rstrip("/")

    def fingerprint(self, origin, release):
        if not release["signed"]:
            if urlsplit(origin).hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("Mise à jour distante non signée.")
            if self.publisher():
                raise ValueError("Cette installation exige une mise à jour signée.")
            return None
        fingerprint = self.publisher()
        if not fingerprint:
            raise ValueError("L’empreinte de l’éditeur doit être configurée par une installation signée.")
        return fingerprint

    def publisher(self):
        for name in ("agent-update-trust.json", "agent-release.json"):
            fingerprint = read_object(self.install / name).get("publisher_sha256")
            if isinstance(fingerprint, str) and SHA256.fullmatch(fingerprint.lower()):
                return fingerprint.lower()
        return None

    @staticmethod
    def powershell(script, *arguments):
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Scope this to the child process; organization Group Policy still applies.
        return [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                "-File", str(script), *map(str, arguments)]

    def verify(self, package, release, fingerprint):
        if read_object(package / "release.json").get("version") != release["version"]:
            raise ValueError("La version du paquet diffère de la version annoncée.")
        if not (package / "OpenNpuAgent" / "OpenNpuAgent.exe").is_file() or not (package / "install-agent.ps1").is_file():
            raise ValueError("Le paquet de mise à jour est incomplet.")
        if fingerprint:
            verifier = Path(__file__).resolve().with_name("verify-agent-package.ps1")
            result = subprocess.run(self.powershell(verifier, "-Package", package, "-PublisherSha256", fingerprint),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise ValueError("La signature du paquet de mise à jour est invalide.")

    def ready(self):
        try:
            release = validate_release(self.state.get("ready"))
            if (self.state.get("origin") == self.connection() and self.archive.is_file()
                    and version_tuple(release["version"]) > self.version):
                return release
        except (OSError, KeyError, TypeError, ValueError):
            pass
        return None

    def check(self, stop, now):
        origin = self.connection()
        checked = self.state.get("checked")
        if (self.state.get("origin") == origin and type(checked) in (int, float)
                and 0 <= now - checked < CHECK_INTERVAL):
            return None
        ready = self.ready()
        self.directory.mkdir(exist_ok=True)
        self.state = {"origin": origin, "checked": now, "ready": ready}
        save_json(self.state_file, self.state)
        with self.open(origin + "/downloads/agent/latest.json") as response:
            release = validate_release(json.loads(response.read(4097)))
        if version_tuple(release["version"]) <= self.version or (ready and version_tuple(release["version"]) <= version_tuple(ready["version"])):
            return None
        fingerprint = self.fingerprint(origin, release)
        with tempfile.TemporaryDirectory(dir=self.directory) as scratch:
            path = Path(scratch) / "release.zip"
            digest = hashlib.sha256()
            size = 0
            with self.open(origin + release["url"]) as response, path.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    if stop.is_set():
                        return None
                    size += len(chunk)
                    if size > release["size"]:
                        raise ValueError("Taille de téléchargement incorrecte.")
                    digest.update(chunk)
                    output.write(chunk)
            if size != release["size"] or digest.hexdigest() != release["sha256"]:
                raise ValueError("Empreinte du téléchargement incorrecte.")
            package = Path(scratch) / "package"
            unpack_release(path, package)
            self.verify(package, release, fingerprint)
            if not stop.is_set():
                path.replace(self.archive)
                self.state = dict(self.state, ready=release)
                save_json(self.state_file, self.state)
                return release
        return None

    def watch(self, stop, notify):
        if not self.installed:
            return
        release = self.ready()
        if release:
            notify(release)
        else:
            self.clear_completed()
        while not stop.is_set():
            try:
                with self.lock:
                    release = self.check(stop, time.time())
                if release:
                    notify(release)
            except HTTPError as error:
                if error.code != 404:
                    logging.warning("Vérification des mises à jour indisponible (HTTP %d).", error.code)
            except (OSError, KeyError, RuntimeError, TypeError, ValueError, BadZipFile, subprocess.TimeoutExpired):
                logging.exception("Impossible de préparer la mise à jour.")
            stop.wait(60)

    def clear_completed(self):
        try:
            release = validate_release(self.state.get("ready"))
            version = release["version"]
            if version_tuple(version) > self.version:
                return
            self.archive.unlink(missing_ok=True)
            parent = self.directory.resolve()
            for child in self.directory.glob(f"install-{version}-*"):
                if child.is_dir() and child.resolve().parent == parent and child.resolve().name == child.name:
                    shutil.rmtree(child)
        except ValueError:
            pass
        except OSError:
            logging.warning("Le nettoyage des fichiers de mise à jour est différé.")

    def install_ready(self, release):
        with self.lock:
            self._install_ready(release)

    def _install_ready(self, release):
        if release != self.ready():
            raise ValueError("La mise à jour prête a changé.")
        fingerprint = self.fingerprint(self.connection(), release)
        with tempfile.TemporaryDirectory(prefix=f"install-{release['version']}-", dir=self.directory) as scratch:
            package = Path(scratch)
            with self.archive.open("rb") as archive:
                if hashlib.file_digest(archive, "sha256").hexdigest() != release["sha256"]:
                    raise ValueError("Le fichier téléchargé a été modifié.")
                archive.seek(0)
                unpack_release(archive, package)
            self.verify(package, release, fingerprint)
            arguments = ["-Update", "-InstallDirectory", self.install]
            if fingerprint:
                arguments.extend(["-PublisherSha256", fingerprint])
            with (self.directory / "install.log").open("ab") as log:
                result = subprocess.run(self.powershell(package / "install-agent.ps1", *arguments), stdout=log,
                                        stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError("L’installation a échoué. Consultez le journal des mises à jour.")
