"""Catalogue approuvé et séquencement des capsules NPU."""

import hashlib
import json
import re
import secrets
import time

from flask import Blueprint, abort, jsonify, request, send_file
from werkzeug.exceptions import Conflict

from capsule_manifest import (CAPSULE_ASSETS, MAX_TENSOR, HEX, asset_digest, next_token, outputs_agree, pack_values,
                              tensor_size, validate_output, verify_manifest)
from network import transfer_profile, upload_seconds

STAGE_LEASE = 300
WORKER_TTL = 60
MAX_ATTEMPTS = 3


def initialize_pipeline(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS capsule_approvals (
            digest TEXT PRIMARY KEY, id TEXT NOT NULL, version INTEGER NOT NULL,
            approved INTEGER NOT NULL, UNIQUE(id,version)
        );
        CREATE TABLE IF NOT EXISTS pipeline_workers (
            node INTEGER PRIMARY KEY REFERENCES nodes(id), fingerprint TEXT NOT NULL, seen REAL NOT NULL,
            formats INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            id TEXT PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id), digest TEXT NOT NULL,
            created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', stage INTEGER NOT NULL DEFAULT 0,
            turn INTEGER NOT NULL DEFAULT 0, prompt TEXT, generated TEXT NOT NULL DEFAULT '[]',
            max_new INTEGER, distinct_nodes INTEGER NOT NULL, input BLOB, output BLOB,
            node INTEGER REFERENCES nodes(id), lease TEXT, expires REAL,
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT
        );
        CREATE INDEX IF NOT EXISTS pipeline_by_status ON pipeline_jobs(status,created);
        CREATE INDEX IF NOT EXISTS pipeline_by_project ON pipeline_jobs(project,created);
        CREATE INDEX IF NOT EXISTS pipeline_by_expiry ON pipeline_jobs(expires) WHERE status='running';
        CREATE INDEX IF NOT EXISTS pipeline_by_node ON pipeline_jobs(node,expires) WHERE status='running';
        CREATE TABLE IF NOT EXISTS pipeline_assignments (
            job TEXT NOT NULL REFERENCES pipeline_jobs(id), stage INTEGER NOT NULL,
            node INTEGER NOT NULL REFERENCES nodes(id), PRIMARY KEY(job,stage)
        );
        CREATE TABLE IF NOT EXISTS pipeline_receipts (
            job TEXT NOT NULL REFERENCES pipeline_jobs(id), lease TEXT NOT NULL, node INTEGER NOT NULL,
            PRIMARY KEY(job,lease)
        );
        CREATE TABLE IF NOT EXISTS pipeline_failures (
            job TEXT NOT NULL REFERENCES pipeline_jobs(id), stage INTEGER NOT NULL,
            turn INTEGER NOT NULL, node INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(job,stage,turn,node)
        );
    """)


def migrate_pipeline(db):
    worker_columns = {column["name"] for column in db.execute("PRAGMA table_info(pipeline_workers)")}
    if "formats" not in worker_columns:
        db.execute("ALTER TABLE pipeline_workers ADD COLUMN formats INTEGER NOT NULL DEFAULT 1")
    columns = {column["name"] for column in db.execute("PRAGMA table_info(pipeline_jobs)")}
    if "verify_results" not in columns:
        db.execute("ALTER TABLE pipeline_jobs ADD COLUMN verify_results INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE pipeline_jobs ADD COLUMN verification_node INTEGER")
        db.execute("ALTER TABLE pipeline_jobs ADD COLUMN verification_output BLOB")


def pipeline_busy(db, node, now):
    return db.execute("""
        SELECT 1 FROM pipeline_jobs WHERE node=? AND status='running' AND expires>?
    """, (node, now)).fetchone() is not None


def signed_bundle(directory, public_key, *, verify_assets=False):
    path = directory / "manifest.json"
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Manifeste trop volumineux.")
    document = json.loads(path.read_text(encoding="utf-8"))
    manifest, digest = verify_manifest(document, public_key)
    if directory.name != digest:
        raise ValueError("Le dossier ne correspond pas à l’empreinte du manifeste.")
    for index, stage in enumerate(manifest["stages"]):
        for extension in CAPSULE_ASSETS[manifest["format"]]:
            asset = directory / f"{index}.{extension}"
            expected = stage[extension]
            if asset.stat().st_size != expected["size"] or (verify_assets and asset_digest(asset) != expected["sha256"]):
                raise ValueError("Fichier de capsule altéré.")
    return document, manifest


def eligible_workers(db, project, fingerprint, now, capsule_format):
    return {row["id"] for row in db.execute("""
        SELECT n.id FROM pipeline_workers w JOIN nodes n ON n.id=w.node
        LEFT JOIN project_nodes pn ON pn.node=n.id
        WHERE w.fingerprint=? AND w.seen>=? AND (w.formats & ?)<>0
          AND n.enabled=1 AND n.revoked=0 AND n.paused=0
          AND (pn.project=? OR (pn.project IS NULL AND (SELECT dedicated FROM projects WHERE id=?)=0))
    """, (fingerprint, now - WORKER_TTL, 1 << (capsule_format - 1), project, project))}


def stage_node(db, job, available, capable, stage_count, caller):
    expected = None
    choices = capable.copy()
    if job["verification_node"] is not None:
        choices.discard(job["verification_node"])
    elif job["distinct_nodes"]:
        if len(available) < stage_count:
            return False
        pins = {row["stage"]: row["node"] for row in db.execute(
            "SELECT stage,node FROM pipeline_assignments WHERE job=?", (job["id"],)
        ) if row["node"] in available}
        expected = pins.pop(job["stage"], None)
        choices.difference_update(pins.values())
    if caller not in choices:
        return False
    failures = {row["node"]: row["failures"] for row in db.execute(
        "SELECT node,failures FROM pipeline_failures WHERE job=? AND stage=? AND turn=?",
        (job["id"], job["stage"], job["turn"])
    )}
    best = min(failures.get(candidate, 0) for candidate in choices)
    if expected in choices and failures.get(expected, 0) == best:
        return caller == expected
    return failures.get(caller, 0) == best


def expire_pipeline(db, now):
    if not db.execute("SELECT 1 FROM pipeline_jobs WHERE status='running' AND expires<=? LIMIT 1",
                      (now,)).fetchone():
        return
    db.execute("""
        INSERT INTO pipeline_failures(job,stage,turn,node)
        SELECT id,stage,turn,node FROM pipeline_jobs WHERE status='running' AND expires<=? AND node IS NOT NULL
        ON CONFLICT(job,stage,turn,node) DO UPDATE SET failures=failures+1
    """, (now,))
    db.execute("""
        UPDATE pipeline_jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
          error=CASE WHEN attempts>=? THEN 'Bail expiré' ELSE NULL END,
          input=CASE WHEN attempts>=? THEN NULL ELSE input END,
          verification_output=CASE WHEN attempts>=? THEN NULL ELSE verification_output END,
          node=NULL,lease=NULL,expires=NULL
        WHERE status='running' AND expires<=?
    """, (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, now))


def install_routes(app, runtime, database, admin, project, node, payload, admission):
    bp = Blueprint("capsule_pipeline", __name__)

    def trust():
        try:
            public = (runtime() / "capsule-signing.pub").read_text(encoding="ascii").strip()
            if not HEX.fullmatch(public):
                raise ValueError("Clé publique invalide")
        except (OSError, ValueError):
            abort(503, description="Clé publique des capsules non configurée.")
        return public

    def folder(digest):
        if not HEX.fullmatch(digest):
            abort(404)
        return runtime() / "capsules" / digest

    def load(digest, *, verify_assets=False):
        try:
            return signed_bundle(folder(digest), trust(), verify_assets=verify_assets)
        except (OSError, ValueError):
            abort(409, description="Manifeste ou artefacts non valides.")

    @bp.get("/admin/capsules")
    def page():
        with database() as db:
            admin(db)
        return app.send_static_file("capsules.html")

    @bp.get("/admin/capsules/data")
    def catalog():
        with database() as db:
            role = admin(db)
            approvals = {row["digest"]: dict(row) for row in db.execute("SELECT * FROM capsule_approvals")}
        root = runtime() / "capsules"
        if not (runtime() / "capsule-signing.pub").is_file():
            return jsonify(capsules=[], owner=role == "owner", configured=False)
        public = trust()
        items = []
        for entry in sorted(root.iterdir()) if root.is_dir() else ():
            if not entry.is_dir() or not HEX.fullmatch(entry.name):
                continue
            try:
                _, manifest = signed_bundle(entry, public)
            except (OSError, ValueError):
                continue
            approval = approvals.pop(entry.name, None)
            items.append(dict(digest=entry.name, id=manifest["id"], version=manifest["version"],
                              kind=manifest["kind"], stages=len(manifest["stages"]), format=manifest["format"],
                              approved=bool(approval and approval["approved"]), available=True))
        items.extend(dict(item, available=False) for item in approvals.values() if item["approved"])
        return jsonify(capsules=items, owner=role == "owner", configured=True)

    @bp.post("/admin/capsules/<digest>/<any(approve,withdraw):action>")
    def approve(digest, action):
        payload()
        with database() as db:
            admin(db, owner=True)
        if not HEX.fullmatch(digest):
            abort(404)
        manifest = load(digest, verify_assets=True)[1] if action == "approve" else None
        with database() as db:
            admin(db, owner=True)
            db.execute("BEGIN IMMEDIATE")
            if action == "withdraw":
                if not db.execute("UPDATE capsule_approvals SET approved=0 WHERE digest=?", (digest,)).rowcount:
                    abort(404)
                db.execute("""
                    UPDATE pipeline_jobs SET status='failed',error='Capsule retirée',input=NULL,verification_output=NULL,
                           node=NULL,lease=NULL,expires=NULL
                    WHERE digest=? AND status IN ('queued','running')
                """, (digest,))
            else:
                bound = db.execute("SELECT digest FROM capsule_approvals WHERE id=? AND version=?",
                                   (manifest["id"], manifest["version"])).fetchone()
                if bound and bound["digest"] != digest:
                    abort(409, description="Cette version est déjà associée à une autre empreinte.")
                db.execute("""
                    INSERT INTO capsule_approvals VALUES (?,?,?,1)
                    ON CONFLICT(digest) DO UPDATE SET approved=1
                """, (digest, manifest["id"], manifest["version"]))
        return jsonify(approved=action == "approve")

    @bp.get("/v2/capsules")
    def api_catalog():
        with database() as db:
            project(db)
            items = [dict(row) for row in db.execute("""
                SELECT id,version,digest FROM capsule_approvals WHERE approved=1 ORDER BY id,version DESC
            """)]
        public = trust() if items else None
        available = []
        for item in items:
            try:
                _, manifest = signed_bundle(folder(item["digest"]), public)
            except (OSError, ValueError):
                continue
            item.update(kind=manifest["kind"], stages=len(manifest["stages"]), format=manifest["format"],
                        input=manifest["stages"][0]["input"], output=manifest["stages"][-1]["output"])
            available.append(item)
        return jsonify(capsules=available)

    @bp.get("/admin/capsules/jobs")
    def admin_jobs():
        with database() as db:
            role = admin(db)
            now = time.time()
            expire_pipeline(db, now)
            rows = db.execute("""
                SELECT j.id,j.status,j.stage,j.turn,j.error,j.created,j.distinct_nodes,
                    j.verification_node IS NOT NULL AS verifying,
                    p.name AS project,c.id AS capsule,c.version,n.name AS node,
                    (SELECT count(*) FROM pipeline_assignments a WHERE a.job=j.id) AS assigned
                FROM pipeline_jobs j JOIN projects p ON p.id=j.project
                JOIN capsule_approvals c ON c.digest=j.digest LEFT JOIN nodes n ON n.id=j.node
                ORDER BY j.created DESC LIMIT 25
            """).fetchall()
            workers = db.execute("SELECT count(*) FROM pipeline_workers WHERE seen>=?",
                                 (now - WORKER_TTL,)).fetchone()[0]
        return jsonify(jobs=[dict(row) for row in rows], workers=workers, writable=role != "reader")

    @bp.post("/v2/jobs")
    def submit():
        data = payload()
        name = data.get("capsule")
        version = data.get("version")
        distinct = data.get("distinct_nodes", True)
        verify = data.get("verify_results", False)
        if (not isinstance(name, str) or type(version) is not int or not 1 <= version <= 1000000
                or type(distinct) is not bool or type(verify) is not bool):
            abort(400, description="Capsule, version ou placement invalide.")
        with database() as db:
            project(db, write=True)
            db.execute("BEGIN IMMEDIATE")
            project_id = project(db, write=True)
            now = time.time()
            if denied := admission(db, project_id, now):
                return denied
            approved = db.execute("SELECT digest FROM capsule_approvals WHERE id=? AND version=? AND approved=1",
                                  (name, version)).fetchone()
            if approved is None:
                abort(404, description="Capsule approuvée introuvable.")
            _, manifest = load(approved["digest"])
            spec = manifest["stages"][0]["input"]
            prompt = None
            maximum = None
            try:
                if manifest["kind"] == "decoder":
                    prompt = data.get("tokens")
                    maximum = data.get("max_new_tokens")
                    context = spec["shape"][1]
                    vocab = manifest["stages"][-1]["output"]["shape"][2]
                    if (not isinstance(prompt, list) or not 1 <= len(prompt) < context or
                        type(maximum) is not int or not 1 <= maximum <= min(64, context - len(prompt)) or
                        any(type(token) is not int or not 0 <= token < vocab for token in prompt)):
                        raise ValueError("Tokens ou longueur demandée hors des limites de la capsule.")
                    raw = pack_values(spec, prompt + [manifest["pad"]] * (context - len(prompt)))
                else:
                    raw = pack_values(spec, data.get("values"))
            except ValueError as error:
                abort(400, description=str(error))
            job = secrets.token_hex(16)
            db.execute("""
                INSERT INTO pipeline_jobs(id,project,digest,created,distinct_nodes,prompt,max_new,input,verify_results)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (job, project_id, approved["digest"], now, distinct,
                  json.dumps(prompt) if prompt is not None else None, maximum, raw, verify))
        return jsonify(id=job, status="queued"), 202

    @bp.get("/v2/jobs/<job_id>")
    def get_job(job_id):
        with database() as db:
            project_id = project(db)
            expire_pipeline(db, time.time())
            row = db.execute("""
                SELECT id,status,stage,turn,generated,error,output IS NOT NULL AS binary_result,
                  distinct_nodes,verify_results,verification_node IS NOT NULL AS verifying,
                  (SELECT count(*) FROM pipeline_assignments a WHERE a.job=j.id) AS assigned
                FROM pipeline_jobs j WHERE id=? AND project=?
            """, (job_id, project_id)).fetchone()
            if row is None:
                abort(404)
            report = dict(row)
            report["tokens"] = json.loads(report.pop("generated"))
            return jsonify(report)

    def cancel(db, job_id, project_id=None):
        row = db.execute("SELECT project,status FROM pipeline_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (project_id is not None and row["project"] != project_id):
            abort(404)
        if row["status"] == "cancelled":
            return
        if row["status"] not in ("queued", "running"):
            abort(409, description="La demande est déjà terminée.")
        db.execute("""
            UPDATE pipeline_jobs SET status='cancelled',input=NULL,verification_output=NULL,verification_node=NULL,
              node=NULL,lease=NULL,expires=NULL,error=NULL WHERE id=?
        """, (job_id,))

    @bp.post("/v2/jobs/<job_id>/cancel")
    def cancel_project_job(job_id):
        with database() as db:
            project(db, write=True)
            db.execute("BEGIN IMMEDIATE")
            cancel(db, job_id, project(db, write=True))
        return jsonify(status="cancelled")

    @bp.post("/admin/capsules/jobs/<job_id>/cancel")
    def cancel_admin_job(job_id):
        payload()
        with database() as db:
            admin(db)
            db.execute("BEGIN IMMEDIATE")
            cancel(db, job_id)
        return jsonify(status="cancelled")

    @bp.get("/v2/jobs/<job_id>/result")
    def get_tensor(job_id):
        with database() as db:
            project_id = project(db)
            row = db.execute("SELECT output FROM pipeline_jobs WHERE id=? AND project=? AND status='done'",
                             (job_id, project_id)).fetchone()
            if row is None or row["output"] is None:
                abort(404)
            return app.response_class(row["output"], mimetype="application/octet-stream")

    @bp.post("/v2/node/claim", defaults={"onnx_only": False})
    @bp.post("/v2/node/claim-onnx", defaults={"onnx_only": True})
    def claim(onnx_only):
        data = payload()
        formats = [2] if onnx_only else data.get("formats", [1])
        if (not isinstance(formats, list) or not 1 <= len(formats) <= len(CAPSULE_ASSETS)
                or any(type(value) is not int or value not in CAPSULE_ASSETS for value in formats)):
            abort(400, description="Formats de capsules invalides.")
        format_mask = sum(1 << (value - 1) for value in set(formats))
        with database() as db:
            node(db)
            required = hashlib.sha256(bytes.fromhex(trust())).hexdigest()
            if data.get("fingerprint") != required:
                abort(403, description="Clé publique de capsule différente.")
            db.execute("BEGIN IMMEDIATE")
            caller = node(db)
            now = time.time()
            expire_pipeline(db, now)
            db.execute("""
                INSERT INTO pipeline_workers(node,fingerprint,seen,formats) VALUES (?,?,?,?) ON CONFLICT(node)
                DO UPDATE SET fingerprint=excluded.fingerprint,seen=excluded.seen,formats=excluded.formats
            """, (caller, required, now, format_mask))
            if pipeline_busy(db, caller, now) or db.execute(
                "SELECT 1 FROM tasks WHERE node=? AND status='running' AND expires>?", (caller, now)
            ).fetchone():
                return jsonify(task=None)
            profiles = {row["node"]: row for row in db.execute("SELECT * FROM node_network")}
            for job in db.execute("""
                SELECT j.id,j.project,j.digest,j.stage,j.turn,j.distinct_nodes,j.verify_results,j.verification_node
                FROM pipeline_jobs j
                JOIN capsule_approvals c ON c.digest=j.digest
                WHERE j.status='queued' AND c.approved=1 ORDER BY j.created,j.id
            """):
                try:
                    document, manifest = load(job["digest"])
                except Conflict:
                    db.execute('''
                        UPDATE pipeline_jobs SET status='failed',error='Capsule non disponible',input=NULL,
                            verification_node=NULL,verification_output=NULL WHERE id=?
                    ''', (job["id"],))
                    continue
                if manifest["format"] not in formats:
                    continue
                available = eligible_workers(db, job["project"], required, now, manifest["format"])
                if caller not in available:
                    continue
                stage = manifest["stages"][job["stage"]]
                stage_input_size, stage_output_size = tensor_size(stage["input"]), tensor_size(stage["output"])
                capable = set()
                for candidate in available:
                    upload, download, _ = transfer_profile(profiles.get(candidate), now)
                    transfer = upload_seconds(stage_output_size, upload) + upload_seconds(stage_input_size, download)
                    if transfer <= STAGE_LEASE * 0.75:
                        capable.add(candidate)
                if job["verify_results"] and job["verification_node"] is None and len(capable) < 2:
                    continue
                if not stage_node(db, job, available, capable, len(manifest["stages"]), caller):
                    continue
                lease = secrets.token_urlsafe(32)
                db.execute("""
                    UPDATE pipeline_jobs SET status='running',node=?,lease=?,expires=?,attempts=attempts+1
                    WHERE id=?
                """, (caller, hashlib.sha256(lease.encode()).hexdigest(), now+STAGE_LEASE, job["id"]))
                if job["verification_node"] is None:
                    db.execute("""
                        INSERT INTO pipeline_assignments VALUES (?,?,?)
                        ON CONFLICT(job,stage) DO UPDATE SET node=excluded.node
                    """, (job["id"], job["stage"], caller))
                return jsonify(task=dict(job=job["id"], stage=job["stage"], turn=job["turn"],
                                         lease=lease, digest=job["digest"], capsule=document))
            return jsonify(task=None)

    def lease_key():
        value = request.headers.get("X-Stage-Lease", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            abort(401)
        return hashlib.sha256(value.encode()).hexdigest()

    def current_job(db, job_id, caller, key):
        row = db.execute("""
            SELECT * FROM pipeline_jobs WHERE id=? AND node=? AND lease=? AND status='running' AND expires>?
        """, (job_id, caller, key, time.time())).fetchone()
        if row is None:
            abort(409, description="Bail périmé.")
        return row

    @bp.get("/v2/node/stages/<job_id>/input")
    def stage_input(job_id):
        with database() as db:
            job = current_job(db, job_id, node(db), lease_key())
            return app.response_class(job["input"], mimetype="application/octet-stream")

    @bp.get("/v2/node/stages/<job_id>/assets/<any(xml,bin,onnx):extension>")
    def artifact(job_id, extension):
        with database() as db:
            job = current_job(db, job_id, node(db), lease_key())
            _, manifest = load(job["digest"])
            if extension not in CAPSULE_ASSETS[manifest["format"]]:
                abort(404)
            file = folder(job["digest"]) / f"{job['stage']}.{extension}"
        if not file.is_file():
            abort(404)
        return send_file(file, mimetype="application/octet-stream")

    @bp.post("/v2/node/stages/<job_id>/fail")
    def stage_failed(job_id):
        with database() as db:
            key = lease_key()
            current_job(db, job_id, node(db), key)
            db.execute("BEGIN IMMEDIATE")
            job = current_job(db, job_id, node(db), key)
            failed = job["attempts"] >= MAX_ATTEMPTS
            db.execute("""
                INSERT INTO pipeline_failures(job,stage,turn,node) VALUES (?,?,?,?)
                ON CONFLICT(job,stage,turn,node) DO UPDATE SET failures=failures+1
            """, (job_id, job["stage"], job["turn"], job["node"]))
            db.execute("""
                UPDATE pipeline_jobs SET status=?,error=?,node=NULL,lease=NULL,expires=NULL,
                    input=CASE WHEN ? THEN NULL ELSE input END,
                    verification_output=CASE WHEN ? THEN NULL ELSE verification_output END WHERE id=?
            """, ("failed" if failed else "queued", "Étape non exécutable sur la NPU", failed, failed, job_id))
            if job["verification_node"] is None:
                db.execute("DELETE FROM pipeline_assignments WHERE job=? AND stage=?", (job_id, job["stage"]))
        return jsonify(status="failed" if failed else "queued")

    @bp.post("/v2/node/stages/<job_id>/result")
    def complete(job_id):
        request.max_content_length = MAX_TENSOR
        key = lease_key()
        with database() as db:
            caller = node(db)
            receipt = "SELECT 1 FROM pipeline_receipts WHERE job=? AND lease=? AND node=?"
            if db.execute(receipt, (job_id, key, caller)).fetchone():
                return jsonify(accepted=True)
            job = current_job(db, job_id, caller, key)
            _, manifest = load(job["digest"])
            stages = manifest["stages"]
            output = stages[job["stage"]]["output"]
            if request.content_length != tensor_size(output):
                abort(400, description="Taille du tenseur de sortie invalide.")
            raw = request.get_data(cache=False)
            try:
                validate_output(output, raw)
            except ValueError as error:
                abort(400, description=str(error))

            db.execute("BEGIN IMMEDIATE")
            caller = node(db)
            if db.execute(receipt, (job_id, key, caller)).fetchone():
                return jsonify(accepted=True)
            job = current_job(db, job_id, caller, key)
            index, turn = job["stage"] + 1, job["turn"]
            generated = json.loads(job["generated"])
            if job["verify_results"]:
                if job["verification_node"] is None:
                    db.execute("INSERT INTO pipeline_receipts VALUES (?,?,?)", (job_id, key, caller))
                    db.execute("""
                        UPDATE pipeline_jobs SET status='queued',verification_node=?,verification_output=?,
                          node=NULL,lease=NULL,expires=NULL,attempts=0,error=NULL WHERE id=?
                    """, (caller, raw, job_id))
                    return jsonify(accepted=True, verifying=True)
                primary = job["verification_output"]
                agree = outputs_agree(output, primary, raw)
                if agree and manifest["kind"] == "decoder" and index == len(stages):
                    position = len(json.loads(job["prompt"])) + len(generated) - 1
                    agree = next_token(output, primary, position) == next_token(output, raw, position)
                if not agree:
                    db.execute("INSERT INTO pipeline_receipts VALUES (?,?,?)", (job_id, key, caller))
                    db.execute("""
                        UPDATE pipeline_jobs SET status='failed',error='Résultats divergents',
                          input=NULL,verification_output=NULL,verification_node=NULL,node=NULL,lease=NULL,expires=NULL
                        WHERE id=?
                    """, (job_id,))
                    return jsonify(accepted=True, verified=False)
                raw = primary
            state = "queued"
            final = None
            if index == len(stages):
                if manifest["kind"] == "tensor":
                    state, final, raw = "done", raw, None
                else:
                    prompt = json.loads(job["prompt"])
                    try:
                        selected = next_token(output, raw, len(prompt) + len(generated) - 1)
                    except ValueError:
                        abort(400, description="Logits non finis.")
                    generated.append(selected)
                    if selected == manifest["eos"] or len(generated) >= job["max_new"]:
                        state, raw = "done", None
                    else:
                        head = stages[0]["input"]
                        prefix = prompt + generated
                        raw = pack_values(head, prefix + [manifest["pad"]] * (head["shape"][1] - len(prefix)))
                        index, turn = 0, turn + 1
            db.execute("INSERT INTO pipeline_receipts VALUES (?,?,?)", (job_id, key, caller))
            db.execute("""
                UPDATE pipeline_jobs SET status=?,stage=?,turn=?,generated=?,input=?,output=?,
                  node=NULL,lease=NULL,expires=NULL,attempts=0,error=NULL,
                  verification_node=NULL,verification_output=NULL WHERE id=?
            """, (state, index, turn, json.dumps(generated), raw, final, job_id))
        return jsonify(accepted=True)

    app.register_blueprint(bp)
