from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from zipfile import ZipFile

from agent_updates import AgentUpdates, CHECK_INTERVAL


class AgentUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "node.json"
        self.origin = "http://127.0.0.1:8787"
        self.requests = []
        self.stop = Event()
        self.make_release()

    def make_release(self, extra=None):
        archive = BytesIO()
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("OpenNpuAgent/OpenNpuAgent.exe", "fixture")
            bundle.writestr("install-agent.ps1", "fixture")
            bundle.writestr("release.json", '{"version":"0.3.4"}')
            if extra:
                bundle.writestr(extra, "fixture")
        self.contents = archive.getvalue()
        self.release = {"version": "0.3.4", "url": "/downloads/agent/OpenNpuAgent-0.3.4-windows-x64.zip",
                        "size": len(self.contents), "signed": False, "sha256": sha256(self.contents).hexdigest()}

    def open(self, url):
        self.requests.append(url.removeprefix(self.origin))
        if url.endswith("latest.json"):
            return BytesIO(json.dumps(self.release).encode())
        if url.endswith(self.release["url"]):
            return BytesIO(self.contents)
        raise HTTPError(url, 404, "Not found", {}, None)

    def updater(self, installed=True):
        return AgentUpdates(self.config, "0.3.3", lambda: {"server": self.origin}, self.open, installed=installed)

    def test_download_is_pending_and_daily_check_survives_restart(self):
        updater = self.updater()
        self.assertEqual(updater.check(self.stop, 100), self.release)
        self.assertEqual(updater.archive.read_bytes(), self.contents)
        self.assertEqual(len(self.requests), 2)
        restarted = self.updater()
        self.assertEqual(restarted.ready(), self.release)
        self.assertIsNone(restarted.check(self.stop, 100 + CHECK_INTERVAL - 1))
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(restarted.check(self.stop, 100 + CHECK_INTERVAL))
        self.assertEqual(len(self.requests), 3)

    def test_confirmation_is_a_separate_operation(self):
        updater = self.updater()
        with patch("agent_updates.subprocess.run") as run:
            self.assertEqual(updater.check(self.stop, 100), self.release)
            run.assert_not_called()
            updater.archive.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "modifié"):
                updater.install_ready(self.release)
            run.assert_not_called()

    def test_remote_unsigned_release_is_never_downloaded(self):
        self.origin = "https://example.invalid"
        updater = self.updater()
        with self.assertRaisesRegex(ValueError, "non signée"):
            updater.check(self.stop, 100)
        self.assertEqual(self.requests, ["/downloads/agent/latest.json"])
        self.assertIsNone(updater.ready())

    def test_traversal_or_bad_digest_cannot_create_a_pending_update(self):
        self.make_release("OpenNpuAgent/../outside")
        updater = self.updater()
        with self.assertRaisesRegex(ValueError, "Chemin"):
            updater.check(self.stop, 100)
        self.make_release()
        self.release["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Empreinte"):
            updater.check(self.stop, 100 + CHECK_INTERVAL)
        self.assertIsNone(updater.ready())

    def test_portable_agent_does_not_download_an_update_it_cannot_apply(self):
        self.updater(installed=False).watch(self.stop, self.fail)
        self.assertEqual(self.requests, [])
