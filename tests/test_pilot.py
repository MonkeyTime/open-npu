import base64
import hmac
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server
from capsule import CAPSULE
from ops.backup import verified_copy
from preferences import pause_reason


class PilotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        password = "isolated-test-" + "x" * 32
        vars = patch.multiple(server, RUNTIME=self.root, PILOT=True, PILOT_ADMIN_HASH=server.digest(password), create=True)
        vars.start()
        self.addCleanup(vars.stop)
        self.admin = ("superadmin", password)
        server.initialize()
        self.client = server.app.test_client()
        with server.database() as db:
            db.execute("INSERT INTO projects(name,description,status,api_key) VALUES ('Test','','approved',?)",
                       (server.digest("project-test"),))
        self.headers = {"Authorization": "Bearer project-test", "Idempotency-Key": "test-request"}
        self.payload = {"capsule": CAPSULE, "values": [-2, 3]}

    def test_invitation_concurrent_limit_and_retry(self):
        response = self.client.post("/admin/enrollment-invites", auth=self.admin,
                                    json={"label": "Campus", "hours": 1, "max_uses": 1})
        self.assertEqual(response.status_code, 201)
        key = response.json["enrollment_key"]
        self.assertFalse((self.root / "enrollment.key").exists())

        def enroll(identity):
            with server.app.test_client() as client:
                return client.post("/v1/node/enroll", headers={"Authorization": f"Bearer {key}"},
                                   json={"name": "Machine", "identity": identity * 43})

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(enroll, "ab"))
        self.assertEqual(sorted(item.status_code for item in results), [200, 401])
        winner = next(identity for identity, result in zip("ab", results) if result.status_code == 200)
        self.assertEqual(enroll(winner).status_code, 200)
        self.assertEqual(self.client.get("/admin/enrollment-invites", auth=self.admin).json["invites"][0]["uses"], 1)
        self.assertEqual(self.client.post("/admin/enrollment-invites/1/revoke", json={}, auth=self.admin).status_code, 200)
        self.assertEqual(enroll("c").status_code, 401)

    def test_hardware_offer_is_private_and_separate_from_projects(self):
        offer = {"affiliation": "Campus A", "contact": "operator@example.org",
                 "node_count": 12, "models": "Intel AI Boost"}
        for count in ("12", True, 0, 100001):
            invalid = self.client.post("/v1/contributions", json=offer | {"node_count": count})
            self.assertEqual(invalid.status_code, 400)
        invalid = self.client.post("/v1/contributions", json=offer | {"contact": "operator"})
        self.assertEqual(invalid.status_code, 400)
        response = self.client.post("/v1/contributions", json=offer)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json), {"id"})
        self.assertEqual(self.client.get("/admin/contributions/data").status_code, 401)
        data = self.client.get("/admin/contributions/data", auth=self.admin).json
        stored, = data["contributions"]
        self.assertEqual((stored["id"], stored["institution"], stored["contact"], stored["node_count"], stored["models"]),
                         (response.json["id"], offer["affiliation"], offer["contact"], offer["node_count"], offer["models"]))
        projects = self.client.get("/admin/projects/data", auth=self.admin).json["projects"]
        self.assertEqual([project["name"] for project in projects], ["Test"])

    def test_public_node_waits_for_fleet_approval(self):
        identity = "p" * 43
        address = "/v1/node/contribute"
        registration = {"identity": identity, "name": "Volunteer workstation"}
        with patch.object(server, "PUBLIC_ENROLLMENT", False):
            self.assertEqual(self.client.post(address, json=registration).status_code, 404)
        with patch.object(server, "PUBLIC_ENROLLMENT", True):
            created = self.client.post(address, json=registration)
            self.assertEqual(created.status_code, 201)
            self.assertTrue(created.json["pending"])
            identifier = created.json["id"]
            self.assertEqual(self.client.post(address, json=registration).json, created.json)
            heartbeat = {"device": "NPU", "capsule": CAPSULE}
            headers = {"Authorization": f"Bearer {identity}"}
            self.assertEqual(self.client.post("/v1/node/claim", headers=headers, json=heartbeat).status_code, 403)
            row, = self.client.get("/admin/fleet?status=disabled", auth=self.admin).json["nodes"]
            self.assertTrue(row["awaiting_approval"])
            self.assertEqual(self.client.post(f"/admin/nodes/{identifier}/enable", json={}, auth=self.admin).status_code, 200)
            self.assertEqual(self.client.post("/v1/node/claim", headers=headers, json=heartbeat).status_code, 200)
            self.assertFalse(self.client.post(address, json=registration).json["pending"])

    def test_pilot_hides_unsigned_agent_release(self):
        folder = self.root / "agent-releases"
        folder.mkdir()
        name = "OpenNpuAgent-0.3.3-windows-x64.zip"
        setup = "OPEN-NPU-Setup-0.3.3-windows-x64.exe"
        content = b"test release"
        (folder / name).write_bytes(content)
        (folder / setup).write_bytes(b"test setup")
        (folder / "latest.json").write_text(json.dumps({
            "version": "0.3.3", "size": len(content), "sha256": server.digest(content.decode()),
            "signed": False,
        }), encoding="utf-8")
        self.assertEqual(self.client.get("/downloads/agent/latest.json").status_code, 404)
        self.assertEqual(self.client.get(f"/downloads/agent/{name}").status_code, 404)
        self.assertEqual(self.client.get(f"/downloads/agent/{setup}").status_code, 404)
        with patch.object(server, "PILOT", False):
            release = self.client.get("/downloads/agent/latest.json").json
            self.assertEqual(release["url"], f"/downloads/agent/{name}")
            self.assertEqual(release["setup_url"], f"/downloads/agent/{setup}")
            with self.client.get(release["setup_url"]) as download:
                self.assertEqual(download.data, b"test setup")
            with self.client.get(release["url"]) as download:
                self.assertEqual(download.data, content)
                self.assertIn("attachment", download.headers["Content-Disposition"])
            self.assertEqual(self.client.get("/downloads/agent/latest.exe").status_code, 404)

    def test_job_retries_conflict_cancellation_and_restoration(self):
        def submit():
            with server.app.test_client() as client:
                return client.post("/v1/jobs", json=self.payload, headers=self.headers)

        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: submit(), range(3)))
        self.assertEqual(len({response.json["id"] for response in results}), 1)
        job = results[0].json["id"]
        conflict = self.client.post("/v1/jobs", json=self.payload | {"values": [9]}, headers=self.headers)
        self.assertEqual(conflict.status_code, 409)
        route = f"/v1/jobs/{job}"
        self.assertEqual(self.client.post(route + "/cancel", headers=self.headers).json["status"], "cancelled")
        self.assertEqual(self.client.post(route + "/cancel", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(route, headers=self.headers).json["status"], "cancelled")
        backup = self.root / "backup.sqlite3"
        restored = self.root / "restored"
        restored.mkdir()
        verified_copy(self.root / "network.sqlite3", backup)
        verified_copy(backup, restored / "network.sqlite3")
        with patch.object(server, "RUNTIME", restored):
            self.assertEqual(self.client.get("/health/ready").status_code, 200)
            self.assertEqual(self.client.get(route, headers=self.headers).json["status"], "cancelled")
        with self.assertRaises(FileExistsError):
            verified_copy(self.root / "network.sqlite3", backup)

    def test_wrong_result_is_not_delivered(self):
        with server.database() as db:
            db.execute("INSERT INTO nodes(name,secret) VALUES ('NPU',?)", (server.digest("node-test"),))
        self.client.post("/v1/jobs", json=self.payload, headers=self.headers)
        headers = {"Authorization": "Bearer node-test"}
        claim = {"device": "NPU", "capsule": CAPSULE}
        lease = self.client.post("/v1/node/claim", json=claim, headers=headers).json["task"]
        bad = self.client.post("/v1/node/result", json=lease | {"values": [1, 2]}, headers=headers)
        self.assertEqual(bad.status_code, 409)
        retry = self.client.post("/v1/node/claim", json=claim, headers=headers).json["task"]
        self.assertNotEqual(lease["lease"], retry["lease"])
        self.assertEqual(self.client.post("/v1/node/result", json=retry | {"values": [4, 9]}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/jobs/{retry['job']}", headers=self.headers).json["values"], [4, 9])

    def test_limits_and_health(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready").status_code, 200)
        with patch.object(server, "PILOT_ACTIVE_JOBS", 1):
            self.assertEqual(self.client.post("/v1/jobs", json=self.payload, headers=self.headers).status_code, 202)
            self.assertEqual(self.client.post("/v1/jobs", json=self.payload, headers=self.headers).status_code, 202)
            denied = self.client.post("/v1/jobs", json=self.payload, headers=self.headers | {"Idempotency-Key": "test-second"})
            self.assertEqual(denied.status_code, 429)
            self.assertIn("Retry-After", denied.headers)

    def test_roles_totp_and_revocation(self):
        response = self.client.post("/admin/access/accounts", auth=self.admin,
                                    json={"name":"lecteur","role":"reader","mfa":True})
        self.assertEqual(response.status_code, 201)
        credentials = response.json
        self.assertEqual(self.client.get("/admin/jobs/data", auth=("lecteur", credentials["password"])).status_code, 401)
        counter = int(time.time() // 30)
        hashed = hmac.digest(base64.b32decode(credentials["totp"]), counter.to_bytes(8, "big"), "sha1")
        offset = hashed[-1] & 15
        code = f"{(int.from_bytes(hashed[offset:offset+4], 'big') & 0x7fffffff) % 1_000_000:06d}"
        with server.app.test_client() as reader:
            data = {"name":"lecteur","password":credentials["password"],"code":code}
            self.assertEqual(reader.post("/admin/session", json=data, base_url="https://localhost").status_code, 200)
            self.assertEqual(reader.post("/admin/session", json=data, base_url="https://localhost").status_code, 401)
            self.assertFalse(reader.get("/admin/jobs/data", base_url="https://localhost").json["writable"])
            self.assertEqual(reader.post("/admin/enrollment-invites", base_url="https://localhost",
                                         json={"label":"Interdit","hours":1,"max_uses":1}).status_code, 403)
            self.assertEqual(self.client.post("/admin/access/accounts/lecteur/revoke", auth=self.admin, json={}).status_code, 200)
            self.assertEqual(reader.get("/admin/jobs/data", base_url="https://localhost").status_code, 401)

    def test_scoped_key_job_retry_and_node_revoke(self):
        response = self.client.post("/admin/projects/1/keys", auth=self.admin,
                                    json={"label":"lecture","scope":"read","days":1})
        headers = {"Authorization": f"Bearer {response.json['api_key']}"}
        job = self.client.post("/v1/jobs", json=self.payload, headers=self.headers).json["id"]
        self.assertEqual(self.client.get(f"/v1/jobs/{job}", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/v1/jobs", json=self.payload, headers=headers).status_code, 403)
        self.assertEqual(self.client.post(f"/v1/jobs/{job}/cancel", headers=headers).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/jobs/{job}/cancel", json={}, auth=self.admin).status_code, 200)
        retry = self.client.post(f"/admin/jobs/{job}/retry", json={}, auth=self.admin)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(self.client.post(f"/admin/jobs/{job}/retry", json={}, auth=self.admin).json["id"], retry.json["id"])
        self.assertEqual(self.client.get("/admin/jobs/data?status=cancelled", auth=self.admin).json["total"], 1)
        self.assertEqual(self.client.post("/admin/projects/1/keys/1/revoke", auth=self.admin, json={}).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/jobs/{job}", headers=headers).status_code, 401)
        with server.database() as db:
            db.execute("INSERT INTO nodes(name,secret) VALUES ('Poste',?)", (server.digest("node"),))
        node_headers = {"Authorization":"Bearer node"}
        heartbeat = {"device":"NPU","capsule":CAPSULE,"agent_version":"0.2.0"}
        self.assertIsNotNone(self.client.post("/v1/node/claim", headers=node_headers, json=heartbeat).json["task"])
        self.assertEqual(self.client.get("/admin/operations/data", auth=self.admin).json["versions"][0]["version"], "0.2.0")
        self.assertEqual(self.client.post("/admin/nodes/1/revoke", auth=self.admin, json={}).json["released"], 1)
        self.assertEqual(self.client.post("/admin/nodes/1/enable", auth=self.admin, json={}).status_code, 409)
        self.assertEqual(self.client.post("/v1/node/claim", headers=node_headers, json=heartbeat).status_code, 401)
        self.assertEqual(self.client.get("/admin/history/data?action=node_revoked", auth=self.admin).json["events"][0]["actor_name"], "superadmin")

    def test_contribution_policy(self):
        settings = {"paused":False,"ac_only":True,"schedule_start":"22:00","schedule_end":"07:00"}
        self.assertIsNone(pause_reason(settings, 60, True))
        self.assertEqual(pause_reason(settings, 480, True), "Hors plage horaire")
        self.assertEqual(pause_reason(settings, 60, False), "En attente du secteur")
        self.assertEqual(pause_reason(settings | {"paused":True}, 60, True), "Partage en pause")

    def test_project_admission_and_priority(self):
        policy = {"admissions": False, "max_active": 2, "per_minute": 10, "priority": 3}
        self.assertEqual(self.client.post("/admin/projects/1/policy", auth=self.admin, json=policy).status_code, 200)
        self.assertEqual(self.client.post("/v1/jobs", json=self.payload, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/admin/projects/1/policy", auth=self.admin,
                                         json=policy | {"admissions": True}).status_code, 200)
        with server.database() as db:
            db.execute("INSERT INTO projects(name,description,status,api_key) VALUES ('Autre','','approved',?)",
                       (server.digest("other"),))
            db.execute("INSERT INTO nodes(name,secret) VALUES ('Poste',?)", (server.digest("node"),))
        self.client.post("/v1/jobs", json=self.payload, headers={"Authorization":"Bearer other"})
        new = self.client.post("/v1/jobs", json=self.payload, headers=self.headers).json["id"]
        claim = self.client.post("/v1/node/claim", json={"device":"NPU","capsule":CAPSULE},
                                 headers={"Authorization":"Bearer node"})
        self.assertEqual(claim.json["task"]["job"], new)

    def test_operations_tracks_active_and_unique_completion(self):
        self.assertEqual(self.client.get("/admin/operations/data").status_code, 401)
        with server.database() as db:
            db.execute("INSERT INTO nodes(name,secret) VALUES ('Poste',?)", (server.digest("monitor-node"),))
        job = self.client.post("/v1/jobs", json=self.payload, headers=self.headers).json["id"]
        headers = {"Authorization": "Bearer monitor-node"}
        lease = self.client.post("/v1/node/claim", json={"device": "NPU", "capsule": CAPSULE},
                                 headers=headers).json["task"]
        snapshot = self.client.get("/admin/operations/data", auth=self.admin).json
        self.assertEqual(snapshot["counts"]["working"], 1)
        self.assertEqual(snapshot["active"][0]["job"], job)
        self.assertNotIn("lease", snapshot["active"][0])
        for _ in range(2):
            self.assertEqual(self.client.post("/v1/node/result", json=lease | {"values": [4, 9]},
                                              headers=headers).status_code, 200)
        snapshot = self.client.get("/admin/operations/data", auth=self.admin).json
        self.assertFalse(snapshot["active"])
        self.assertEqual(snapshot["counts"]["online"], 1)
        self.assertEqual(sum(point["completed"] for point in snapshot["trend"]), 1)
