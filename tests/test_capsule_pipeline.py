import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import server
from capsule_manifest import verify_manifest
from ops.capsule_tool import publish, sign


class CapsulePipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        override = patch.object(server, "RUNTIME", self.state)
        override.start()
        self.addCleanup(override.stop)
        server.initialize()
        self.admin = ("superadmin", (self.state / "superadmin.txt").read_text().strip())
        self.client = server.app.test_client()
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes_raw()
        self.fingerprint = hashlib.sha256(public).hexdigest()
        (self.state / "capsule-signing.pub").write_text(public.hex())
        self.source = self.root / "bundle"
        self.source.mkdir()
        tokens = {"dtype": "i32", "shape": [1, 5]}
        hidden = {"dtype": "f16", "shape": [1, 5, 2]}
        scores = {"dtype": "f16", "shape": [1, 5, 8]}
        manifest = {"format": 1, "id": "test-decoder", "version": 1, "kind": "decoder", "pad": 0, "eos": 7,
                    "stages": [{"input": tokens, "output": hidden}, {"input": hidden, "output": scores}]}
        (self.source / "definition.json").write_text(json.dumps(manifest))
        for stage in range(2):
            for ext in ("xml", "bin"):
                (self.source / f"{stage}.{ext}").write_bytes(f"{stage}-{ext}".encode())
        self.digest = sign(self.source, self.key)
        with server.database() as db:
            db.execute("INSERT INTO projects(name,description,status,api_key) VALUES ('Test','','approved',?)",
                       (server.digest("research"),))
            db.executemany("INSERT INTO nodes(name,secret) VALUES (?,?)",
                           [(f"Node {name}", server.digest(name)) for name in ("alice", "bob")])

    def headers(self, name, lease=None):
        result = {"Authorization": f"Bearer {name}"}
        if lease is not None:
            result["X-Stage-Lease"] = lease
        return result

    def approve(self):
        self.assertEqual(publish(self.source, self.state), self.digest)
        response = self.client.post(f"/admin/capsules/{self.digest}/approve", auth=self.admin, json={})
        self.assertEqual(response.status_code, 200)

    def claim(self, node, path="/v2/node/claim", **capabilities):
        response = self.client.post(path, headers=self.headers(node),
                                    json={"fingerprint": self.fingerprint, **capabilities})
        self.assertEqual(response.status_code, 200)
        return response.json["task"]

    def test_capsule_formats_are_routed_to_compatible_workers(self):
        source = self.root / "onnx"
        source.mkdir()
        spec = {"dtype": "f32", "shape": [1, 2]}
        manifest = {"format": 2, "id": "test-onnx", "version": 1, "kind": "tensor",
                    "stages": [{"input": spec, "output": spec}]}
        (source / "definition.json").write_text(json.dumps(manifest))
        model = b"signed ONNX bytes"
        (source / "0.onnx").write_bytes(model)
        digest = sign(source, self.key)
        self.assertEqual(publish(source, self.state), digest)
        self.assertEqual(self.client.post(f"/admin/capsules/{digest}/approve", auth=self.admin, json={}).status_code, 200)
        response = self.client.post("/v2/jobs", headers=self.headers("research"),
                                    json={"capsule": "test-onnx", "version": 1, "values": [2, 5]})
        self.assertEqual(response.status_code, 202)
        onnx_job = response.json["id"]
        self.assertIsNone(self.claim("bob"))
        legacy_job = self.submit_test_job()

        heartbeat = self.client.post("/v1/node/claim", headers=self.headers("alice"), json={"device": "AMD Ryzen AI"})
        self.assertIsNone(heartbeat.json["task"])
        task = self.claim("alice", path="/v2/node/claim-onnx")
        self.assertEqual(task["job"], onnx_job)
        headers = self.headers("alice", task["lease"])
        path = f"/v2/node/stages/{onnx_job}"
        with self.client.get(path + "/assets/onnx", headers=headers) as asset:
            self.assertEqual(asset.data, model)
        self.assertEqual(self.client.get(path + "/assets/xml", headers=headers).status_code, 404)
        self.assertEqual(self.client.post(path + "/result", headers=headers, data=struct.pack("<2f", 4, 10)).status_code, 200)
        self.assertIsNone(self.claim("alice", formats=[2]))
        self.assertEqual(self.claim("bob")["job"], legacy_job)
        self.assertEqual(self.client.post("/admin/projects/1/reservation", auth=self.admin,
                                         json={"count": 1, "node_ids": [1]}).status_code, 200)
        response = self.client.post("/v2/jobs", headers=self.headers("research"),
                                    json={"capsule": "test-onnx", "version": 1, "values": [2, 5]})
        self.assertEqual(response.status_code, 202)
        self.assertIsNone(self.claim("bob"))
        self.assertEqual(self.claim("alice", formats=[2])["job"], response.json["id"])

    def test_signature_and_approval_distinct_nodes_two_tokens(self):
        spec = {"capsule": "test-decoder", "version": 1, "tokens": [2], "max_new_tokens": 2}
        self.assertEqual(self.client.post("/v2/jobs", headers=self.headers("research"), json=spec).status_code, 404)
        self.approve()
        response = self.client.post("/v2/jobs", headers=self.headers("research"), json=spec)
        self.assertEqual(response.status_code, 202)
        job = response.json["id"]
        self.assertIsNone(self.claim("alice"))
        # Le premier appel de Bob annonce le deuxième poste, puis peut prendre le premier étage.
        task = self.claim("bob")
        self.assertEqual((task["stage"], task["turn"]), (0, 0))
        root = f"/v2/node/stages/{job}"
        self.assertEqual(self.client.get(root + "/input", headers=self.headers("alice", task["lease"])).status_code, 409)
        with self.client.get(root + "/assets/xml", headers=self.headers("bob", task["lease"])) as asset:
            self.assertEqual(asset.data, b"0-xml")
        for position in (0, 1):
            self.assertEqual((task["stage"], task["turn"]), (0, position))
            data = struct.pack("<10e", *([1.0] * 10))
            if position == 1:
                encoded = self.client.get(root + "/input", headers=self.headers("bob", task["lease"])).data
                self.assertEqual(struct.unpack("<5i", encoded), (2, 3, 0, 0, 0))
            for _ in range(2):
                self.assertEqual(self.client.post(root + "/result", headers=self.headers("bob", task["lease"]),
                                                  data=data).status_code, 200)
            self.assertIsNone(self.claim("bob"))
            task = self.claim("alice")
            self.assertEqual((task["stage"], task["turn"]), (1, position))
            self.assertEqual(self.client.get(root + "/input", headers=self.headers("alice", task["lease"])).data, data)
            logits = [0.0] * 40
            logits[position * 8 + (3 if position == 0 else 7)] = 20
            response = self.client.post(root + "/result", headers=self.headers("alice", task["lease"]),
                                        data=struct.pack("<40e", *logits))
            self.assertEqual(response.status_code, 200)
            if position == 0:
                self.assertIsNone(self.claim("alice"))
                task = self.claim("bob")
        result = self.client.get(f"/v2/jobs/{job}", headers=self.headers("research")).json
        self.assertEqual((result["status"], result["tokens"], result["assigned"]), ("done", [3, 7], 2))

    def test_reject_altered_assets_and_manifest_and_withdraw_open_job(self):
        doc = json.loads((self.source / "manifest.json").read_text())
        doc["manifest"]["version"] = 2
        with self.assertRaises(ValueError):
            verify_manifest(doc, self.key.public_key().public_bytes_raw().hex())
        (self.source / "0.xml").write_bytes(b"wrong")
        with self.assertRaises(ValueError):
            publish(self.source, self.state)
        (self.source / "0.xml").write_bytes(b"0-xml")
        self.approve()
        path = f"/admin/capsules/{self.digest}/withdraw"
        response = self.client.post("/v2/jobs", headers=self.headers("research"),
                                    json={"capsule": "test-decoder", "version": 1, "tokens": [2], "max_new_tokens": 1})
        job = response.json["id"]
        self.assertEqual(self.client.post(path, auth=self.admin, json={}).status_code, 200)
        self.assertEqual(self.client.get(f"/v2/jobs/{job}", headers=self.headers("research")).json["status"], "failed")

    def submit_test_job(self, **options):
        self.approve()
        response = self.client.post("/v2/jobs", headers=self.headers("research"),
                                    json={"capsule": "test-decoder", "version": 1, "tokens": [2],
                                          "max_new_tokens": 1, "distinct_nodes": False, **options})
        self.assertEqual(response.status_code, 202)
        return response.json["id"]

    def test_revoke_last_dedicated_node_does_not_open_shared_pool(self):
        heartbeat = {"device": "NPU", "capsule": server.CAPSULE}
        for worker in ("alice", "bob"):
            self.client.post("/v1/node/claim", headers=self.headers(worker), json=heartbeat)
        reservation = "/admin/projects/1/reservation"
        self.assertEqual(self.client.post(reservation, auth=self.admin,
                                          json={"count": 1, "node_ids": [1]}).status_code, 200)
        job = self.submit_test_job()
        self.assertEqual(self.client.post("/admin/nodes/1/revoke", auth=self.admin, json={}).status_code, 200)
        self.assertIsNone(self.claim("bob"))
        listing = self.client.get("/admin/projects/data?reservation=reserved", auth=self.admin).json
        self.assertEqual([(p["dedicated"], p["reserved"]) for p in listing["projects"]], [(1, 0)])
        self.assertEqual(self.client.post(reservation, auth=self.admin, json={"count": 0}).status_code, 200)
        self.assertEqual(self.claim("bob")["job"], job)

    def test_expiry_reassigns_and_cancel_invalidates_lease(self):
        job = self.submit_test_job()
        expired = self.claim("alice")
        self.assertIsNone(self.claim("bob"))
        with server.database() as db:
            db.execute("UPDATE pipeline_jobs SET expires=0 WHERE id=?", (job,))
        path = f"/v2/jobs/{job}"
        self.assertEqual(self.client.get(path, headers=self.headers("research")).json["status"], "queued")
        self.assertIsNone(self.claim("alice"))
        replacement = self.claim("bob")
        self.assertNotEqual(expired["lease"], replacement["lease"])
        for _ in range(2):
            self.assertEqual(self.client.post(path + "/cancel", headers=self.headers("research")).status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers("research")).json["status"], "cancelled")
        stage = f"/v2/node/stages/{job}/input"
        self.assertEqual(self.client.get(stage, headers=self.headers("bob", replacement["lease"])).status_code, 409)

    def test_verification_rejects_nonfinite_and_divergent_results(self):
        job = self.submit_test_job(verify_results=True)
        path = f"/v2/node/stages/{job}/result"
        self.assertIsNone(self.claim("alice"))
        primary = self.claim("bob")
        headers = self.headers("bob", primary["lease"])
        invalid = struct.pack("<10e", float("nan"), *([1.0] * 9))
        self.assertEqual(self.client.post(path, headers=headers, data=invalid).status_code, 400)
        valid = struct.pack("<10e", *([1.0] * 10))
        self.assertTrue(self.client.post(path, headers=headers, data=valid).json["verifying"])
        self.assertIsNone(self.claim("bob"))
        verifying = self.claim("alice")
        self.assertEqual(verifying["stage"], primary["stage"])
        mismatch = struct.pack("<10e", *([2.0] * 10))
        response = self.client.post(path, headers=self.headers("alice", verifying["lease"]), data=mismatch)
        self.assertFalse(response.json["verified"])
        self.assertEqual(self.client.get(f"/v2/jobs/{job}", headers=self.headers("research")).json["status"], "failed")

    def test_catalog_skips_one_unavailable_capsule(self):
        self.approve()
        absent = "a" * 64
        with server.database() as db:
            db.execute("INSERT INTO capsule_approvals VALUES (?,?,?,1)", (absent, "absent", 1))
        catalog = self.client.get("/v2/capsules", headers=self.headers("research"))
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual([item["digest"] for item in catalog.json["capsules"]], [self.digest])
        admin = self.client.get("/admin/capsules/data", auth=self.admin).json
        missing, = [item for item in admin["capsules"] if item["digest"] == absent]
        self.assertFalse(missing["available"])

    def test_operations_includes_v2_active_assignments(self):
        job = self.submit_test_job()
        self.assertEqual(self.claim("alice")["job"], job)
        operations = self.client.get("/admin/operations/data", auth=self.admin)
        self.assertEqual(operations.status_code, 200)
        self.assertEqual(operations.json["pipeline"], {"running": 1})
        active, = operations.json["active"]
        self.assertEqual((active["kind"], active["job"]), ("v2", job))

    def test_invalid_credentials_do_not_acquire_a_write_lock(self):
        with server.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(self.client.post("/v1/node/claim", headers=self.headers("invalid"),
                                              json={"capsule": server.CAPSULE, "device": "NPU"}).status_code, 401)
            self.assertEqual(self.client.post("/v2/node/claim", headers=self.headers("invalid"),
                                              json={"fingerprint": self.fingerprint}).status_code, 401)
            self.assertEqual(self.client.post("/admin/session", json={"name": "invalid", "password": "bad"}).status_code, 401)

    def test_pilot_rate_limits_login_but_keeps_other_clients_available(self):
        with patch.object(server, "PILOT", True), patch.object(server, "rate_buckets", server.OrderedDict()):
            for _ in range(12):
                self.assertEqual(self.client.post("/admin/session", json={}).status_code, 400)
            limited = self.client.post("/admin/session", json={})
            self.assertEqual(limited.status_code, 429)
            self.assertGreater(int(limited.headers["Retry-After"]), 0)
            self.assertEqual(self.client.post("/admin/session", json={},
                                              environ_overrides={"REMOTE_ADDR": "198.51.100.5"}).status_code, 400)

    def test_v2_accepts_binary_at_the_tensor_limit(self):
        limit = server.MAX_TENSOR
        original = self.source
        self.source = self.root / "tensor"
        self.source.mkdir()
        for extension in ("xml", "bin"):
            (self.source / f"0.{extension}").write_bytes((original / f"0.{extension}").read_bytes())
        manifest_path = self.source / "definition.json"
        manifest_path.write_text(json.dumps({
            "format": 1, "id": "test-tensor", "version": 1, "kind": "tensor",
            "stages": [{"input": {"dtype": "i32", "shape": [1]},
                        "output": {"dtype": "i32", "shape": [limit // 4]}}],
        }))
        self.digest = sign(self.source, self.key)
        self.approve()
        job = self.client.post("/v2/jobs", headers=self.headers("research"),
                               json={"capsule": "test-tensor", "version": 1, "values": [1]}).json["id"]
        task = self.claim("alice")
        upload = f"/v2/node/stages/{job}/result"
        headers = self.headers("alice", task["lease"])
        self.assertEqual(self.client.post(upload, headers=headers, data=bytes(limit + 1)).status_code, 400)
        self.assertEqual(self.client.post(upload, headers=headers, data=bytes(limit)).status_code, 200)
        result = self.client.get(f"/v2/jobs/{job}/result", headers=self.headers("research"))
        self.assertEqual((result.status_code, len(result.data)), (200, limit))
