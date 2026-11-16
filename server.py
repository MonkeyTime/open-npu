"""Portail et API OPEN-NPU."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import time
from collections import defaultdict
from contextlib import contextmanager

from flask import Flask, abort, jsonify, request
from waitress import serve

from capsule import CAPSULE, MAX_VALUES, WIDTH, valid_values
from network import (MAX_RATE, MIN_UPLOAD_LIMIT, PROBE_BYTES, PROBE_INTERVAL, SAMPLE_VALID_SECONDS,
                     fits_lease, optional_number, transfer_profile)

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("OPEN_NPU_RUNTIME", ROOT / "runtime"))
RUNTIME.mkdir(parents=True, exist_ok=True)
MODE = os.environ.get("OPEN_NPU_MODE", "local")
if MODE not in ("local", "pilot"):
    raise RuntimeError("OPEN_NPU_MODE doit être local ou pilot.")
PILOT = MODE == "pilot"
app = Flask(__name__, static_folder=str(ROOT / "web"), static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 96 * 1024
if PILOT:
    trusted_hosts = os.environ.get("OPEN_NPU_TRUSTED_HOSTS", "").split(",")
    if not all(host.strip() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host.strip()) for host in trusted_hosts):
        raise RuntimeError("Indiquez les noms d’hôte exacts dans OPEN_NPU_TRUSTED_HOSTS.")
    app.config["TRUSTED_HOSTS"] = [host.strip() for host in trusted_hosts]
    admin_file = os.environ.get("OPEN_NPU_ADMIN_SECRET_FILE")
    if not admin_file:
        raise RuntimeError("OPEN_NPU_ADMIN_SECRET_FILE est requis pour le pilote.")
    pilot_password = Path(admin_file).read_text(encoding="utf-8").strip()
    if len(pilot_password) < 32:
        raise RuntimeError("Le secret du pilote doit contenir au moins 32 caractères aléatoires.")
    PILOT_ADMIN_HASH = hashlib.sha256(pilot_password.encode()).hexdigest()
    del pilot_password
LEASE_SECONDS = 120
PILOT_ACTIVE_JOBS = 20
PILOT_JOBS_PER_MINUTE = 30
PROBE_BODY = secrets.token_bytes(PROBE_BYTES)
ONLINE_SECONDS = 25
MAX_BULK_NODES = 100
PAGE_SIZES = (25, 50, 100)
NODE_STATES = ("working", "online", "reserved", "paused", "offline", "new", "disabled")
HISTORY_ACTIONS = ("node_enrolled", "node_created", "enable", "disable", "project_approved",
                   "project_rejected", "project_updated", "key_rotated", "nodes_reserved", "reservation_released")


@contextmanager
def database():
    conn = sqlite3.connect(RUNTIME / "network.sqlite3", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def initialize():
    with database() as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', api_key TEXT UNIQUE,
                affiliation TEXT, contact TEXT
            );
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, secret TEXT NOT NULL UNIQUE,
                seen REAL, device TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                hardware TEXT, capsule TEXT, paused INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS project_nodes (
                node INTEGER PRIMARY KEY REFERENCES nodes(id),
                project INTEGER NOT NULL REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS project_nodes_by_project ON project_nodes(project);
            CREATE TABLE IF NOT EXISTS node_network (
                node INTEGER PRIMARY KEY REFERENCES nodes(id),
                upload_limit_mbps REAL, upload_mbps REAL, download_mbps REAL, rtt_ms REAL,
                sampled_at REAL, local_link INTEGER,
                last_upload_probe REAL, last_download_probe REAL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id), created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_by_project ON jobs(project, created DESC);
            CREATE TABLE IF NOT EXISTS job_requests (
                project INTEGER NOT NULL REFERENCES projects(id),
                request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                job TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(project,request_key)
            );
            CREATE TABLE IF NOT EXISTS job_cancellations (
                job TEXT PRIMARY KEY REFERENCES jobs(id), created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS enrollment_invites (
                id INTEGER PRIMARY KEY, label TEXT NOT NULL, secret TEXT NOT NULL UNIQUE,
                expires REAL NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS invite_nodes (
                node INTEGER PRIMARY KEY REFERENCES nodes(id), invite INTEGER NOT NULL REFERENCES enrollment_invites(id)
            );
            CREATE TABLE IF NOT EXISTS fleet_events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL,
                node_ids TEXT NOT NULL, released INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, quantity INTEGER, released INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS history_by_action ON history_events(action,id DESC);
            CREATE INDEX IF NOT EXISTS history_by_date ON history_events(created);
            CREATE TABLE IF NOT EXISTS history_nodes (
                event INTEGER NOT NULL REFERENCES history_events(id), node INTEGER NOT NULL REFERENCES nodes(id),
                PRIMARY KEY(event,node)
            );
            CREATE TABLE IF NOT EXISTS history_projects (
                event INTEGER NOT NULL REFERENCES history_events(id), project INTEGER NOT NULL REFERENCES projects(id),
                PRIMARY KEY(event,project)
            );
            CREATE INDEX IF NOT EXISTS history_by_node ON history_nodes(node,event);
            CREATE INDEX IF NOT EXISTS history_by_project ON history_projects(project,event);
            CREATE TABLE IF NOT EXISTS tasks (
                job TEXT REFERENCES jobs(id), position INTEGER, input TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', node INTEGER REFERENCES nodes(id),
                lease TEXT, expires REAL, attempts INTEGER NOT NULL DEFAULT 0, output TEXT,
                PRIMARY KEY(job, position)
            );
            CREATE INDEX IF NOT EXISTS tasks_queued ON tasks(job, position) WHERE status='queued';
            CREATE INDEX IF NOT EXISTS tasks_node ON tasks(node) WHERE status='running';
            CREATE INDEX IF NOT EXISTS tasks_expires ON tasks(expires) WHERE status='running';
            CREATE INDEX IF NOT EXISTS tasks_failed ON tasks(job) WHERE status='failed';
        """)
        db.execute("BEGIN IMMEDIATE")
        columns = {column["name"] for column in db.execute("PRAGMA table_info(projects)")}
        if "affiliation" not in columns:
            db.execute("ALTER TABLE projects ADD COLUMN affiliation TEXT")
        if "contact" not in columns:
            db.execute("ALTER TABLE projects ADD COLUMN contact TEXT")
        node_columns = {column["name"] for column in db.execute("PRAGMA table_info(nodes)")}
        if "hardware" not in node_columns:
            db.execute("ALTER TABLE nodes ADD COLUMN hardware TEXT")
        if "capsule" not in node_columns:
            db.execute("ALTER TABLE nodes ADD COLUMN capsule TEXT")
        if "paused" not in node_columns:
            db.execute("ALTER TABLE nodes ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        if not db.execute("SELECT 1 FROM settings WHERE name='history_migrated'").fetchone():
            for event in db.execute("SELECT * FROM fleet_events ORDER BY id"):
                ids = json.loads(event["node_ids"])
                record_history(db, event["action"], nodes=ids, quantity=len(ids),
                               released=event["released"], created=event["created"])
            db.execute("INSERT INTO settings VALUES ('history_migrated','1')")
        cancel_remaining(db)
        if PILOT:
            return
        enrollment = RUNTIME / "enrollment.key"
        if not enrollment.exists():
            with enrollment.open("x", encoding="utf-8") as output:
                output.write(token())
        credentials = RUNTIME / "superadmin.txt"
        if not db.execute("SELECT 1 FROM settings WHERE name='admin'").fetchone():
            if not credentials.exists():
                with credentials.open("x", encoding="utf-8") as output:
                    output.write(token())
            password = credentials.read_text(encoding="utf-8").strip()
            if not password:
                raise RuntimeError("Le fichier du super administrateur est vide.")
            db.execute("INSERT INTO settings VALUES ('admin', ?)", (digest(password),))


def admin(db):
    auth = request.authorization
    expected = PILOT_ADMIN_HASH if PILOT else db.execute("SELECT value FROM settings WHERE name='admin'").fetchone()[0]
    if not auth or auth.type != "basic" or auth.username != "superadmin" or not secrets.compare_digest(
        digest(auth.password or ""), expected
    ):
        abort(401)


def bearer():
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        abort(401)
    return digest(value)


def record_history(db, action, *, actor="admin", nodes=(), projects=(), quantity=None, released=0, created=None):
    event_id = db.execute("""
        INSERT INTO history_events(created,actor,action,quantity,released) VALUES (?,?,?,?,?)
    """, (time.time() if created is None else created, actor, action, quantity, released)).lastrowid
    db.executemany("INSERT INTO history_nodes(event,node) VALUES (?,?)",
                   ((event_id, node_id) for node_id in sorted(set(nodes))))
    db.executemany("INSERT INTO history_projects(event,project) VALUES (?,?)",
                   ((event_id, project_id) for project_id in sorted(set(projects))))


def history_entries(db, events):
    entries = {row["id"]: dict(row, nodes=[], projects=[]) for row in events}
    if not entries:
        return []
    marks = ",".join("?" for _ in entries)
    for row in db.execute(f"""
        SELECT h.event,n.id,n.name FROM history_nodes h JOIN nodes n ON n.id=h.node
        WHERE h.event IN ({marks}) ORDER BY n.id
    """, tuple(entries)):
        entries[row["event"]]["nodes"].append({"id": row["id"], "name": row["name"]})
    for row in db.execute(f"""
        SELECT h.event,p.id,p.name FROM history_projects h JOIN projects p ON p.id=h.project
        WHERE h.event IN ({marks}) ORDER BY p.id
    """, tuple(entries)):
        entries[row["event"]]["projects"].append({"id": row["id"], "name": row["name"]})
    return list(entries.values())


def project(db):
    row = db.execute("SELECT id FROM projects WHERE status='approved' AND api_key=?", (bearer(),)).fetchone()
    if row is None:
        abort(401)
    return row["id"]


def node(db):
    row = db.execute("SELECT id,enabled FROM nodes WHERE secret=?", (bearer(),)).fetchone()
    if row is None:
        abort(401)
    if not row["enabled"]:
        abort(403, description="Nœud désactivé par l’administration.")
    return row["id"]


def payload():
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400)
    return data


def field(data, key, limit):
    value = data.get(key)
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        abort(400, description=f"Champ invalide ({key}).")
    return value.strip()


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
@app.errorhandler(415)
@app.errorhandler(429)
def show_error(error):
    response = jsonify(error=error.description)
    if error.code == 401 and request.path.startswith("/admin"):
        response.headers["WWW-Authenticate"] = 'Basic realm="OPEN-NPU"'
    return response, error.code


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.get("/health/live")
def liveness():
    return jsonify(status="ok")


@app.get("/health/ready")
def readiness():
    try:
        with database() as db:
            db.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()
    except sqlite3.Error:
        return jsonify(status="unavailable"), 503
    return jsonify(status="ready")


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.get("/admin")
def dashboard():
    with database() as db:
        admin(db)
    return app.send_static_file("admin.html")


@app.get("/admin/history")
def history_page():
    with database() as db:
        admin(db)
    return app.send_static_file("history.html")


@app.get("/admin/history/data")
def history_list():
    action = request.args.get("action", "")
    actor = request.args.get("actor", "")
    page = request.args.get("page", 1, type=int)
    size = request.args.get("size", 25, type=int)
    if (action not in ("", *HISTORY_ACTIONS) or actor not in ("", "admin", "agent") or
            not 1 <= page <= 1_000_000 or size not in PAGE_SIZES):
        abort(400, description="Filtres ou pagination invalides.")
    filters = ["(?='' OR e.action=?)", "(?='' OR e.actor=?)"]
    params = [action, action, actor, actor]
    bounds = {}
    for name, operator in (("from", ">="), ("until", "<")):
        value = request.args.get(name)
        if value is not None:
            if len(value) > 13 or not re.fullmatch(r"-?[0-9]+", value):
                abort(400, description="Date invalide.")
            bounds[name] = int(value)
            filters.append(f"e.created {operator} ?")
            params.append(bounds[name])
    if "from" in bounds and "until" in bounds and bounds["from"] >= bounds["until"]:
        abort(400, description="La date de fin doit suivre la date de début.")
    for name, table in (("node", "nodes"), ("project", "projects")):
        value = request.args.get(name, "").strip().casefold()
        if len(value) > 320:
            abort(400, description="Terme de recherche trop long.")
        if value:
            match = "CAST(r.id AS TEXT)=?" if re.fullmatch(r"[0-9]+", value) else "instr(fold(r.name),?)>0"
            filters.append(f"""EXISTS (
                SELECT 1 FROM history_{table} h JOIN {table} r ON r.id=h.{name}
                WHERE h.event=e.id AND {match}
            )""")
            params.append(value)
    where = " WHERE " + " AND ".join(filters)
    with database() as db:
        admin(db)
        db.create_function("fold", 1, str.casefold, deterministic=True)
        db.execute("BEGIN")
        total = db.execute("SELECT count(*) FROM history_events e" + where, params).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        events = db.execute("SELECT e.* FROM history_events e" + where + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
                            (*params, size, (page - 1) * size))
        return jsonify(events=history_entries(db, events), total=total, page=page, pages=pages)


def project_fields(data):
    affiliation = None if data.get("affiliation") in (None, "") else field(data, "affiliation", 150)
    contact = None if data.get("contact") in (None, "") else field(data, "contact", 254)
    if contact is not None and not re.fullmatch(r"[^\s@]+@[^\s@]+", contact):
        abort(400, description="Adresse de contact invalide.")
    return field(data, "name", 100), field(data, "description", 3000), affiliation, contact


@app.post("/v1/projects")
def submit_project():
    values = project_fields(payload())
    with database() as db:
        cursor = db.execute("INSERT INTO projects(name,description,affiliation,contact) VALUES (?,?,?,?)", values)
        return jsonify(id=cursor.lastrowid, status="pending"), 201


@app.get("/admin/projects")
def projects_page():
    with database() as db:
        admin(db)
    return app.send_static_file("projects.html")


@app.get("/admin/projects/<int:project_id>")
def project_page(project_id):
    with database() as db:
        admin(db)
        read_project(db, project_id)
    return app.send_static_file("project.html")


def read_project(db, project_id):
    row = None if project_id > 2**63 - 1 else db.execute("""
        SELECT id,name,description,status,affiliation,contact,api_key IS NOT NULL AS has_key
        FROM projects WHERE id=?
    """, (project_id,)).fetchone()
    if row is None:
        abort(404, description="Projet introuvable.")
    return dict(row)


@app.get("/admin/projects/data")
def project_list():
    search = request.args.get("q", "").strip().casefold()
    status = request.args.get("status", "")
    reservation = request.args.get("reservation", "")
    sort = request.args.get("sort", "recent")
    page = request.args.get("page", 1, type=int)
    size = request.args.get("size", 25, type=int)
    orders = {"recent": "p.id DESC", "name": "fold(p.name), p.id DESC"}
    if (len(search) > 320 or status not in ("", "pending", "approved", "rejected") or
            reservation not in ("", "reserved", "shared") or sort not in orders or
            size not in PAGE_SIZES or not 1 <= page <= 1_000_000):
        abort(400, description="Filtres ou pagination invalides.")
    conditions = ["(?='' OR p.status=?)"]
    params = [status, status]
    if search:
        conditions.append("""(instr(fold(p.name),?)>0 OR instr(fold(coalesce(p.affiliation,'')),?)>0
            OR instr(fold(coalesce(p.contact,'')),?)>0 OR CAST(p.id AS TEXT)=?)""")
        params.extend([search] * 4)
    if reservation:
        exists = "EXISTS (SELECT 1 FROM project_nodes WHERE project=p.id)"
        conditions.append(exists if reservation == "reserved" else "NOT " + exists)
    where = " WHERE " + " AND ".join(conditions)
    with database() as db:
        admin(db)
        db.create_function("fold", 1, str.casefold, deterministic=True)
        db.execute("BEGIN")
        counts = {row["status"]: row["total"] for row in db.execute(
            "SELECT status,count(*) AS total FROM projects GROUP BY status"
        )}
        total = db.execute("SELECT count(*) FROM projects p" + where, params).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        rows = db.execute("""
            SELECT p.id,p.name,p.status,p.affiliation,p.contact,
                (SELECT count(*) FROM project_nodes WHERE project=p.id) AS reserved,
                (SELECT count(*) FROM jobs WHERE project=p.id) AS jobs
            FROM projects p
        """ + where + f" ORDER BY {orders[sort]} LIMIT ? OFFSET ?", (*params, size, (page - 1) * size))
        return jsonify(projects=[dict(row) for row in rows], total=total, page=page, pages=pages, counts=counts)


@app.get("/admin/projects/<int:project_id>/data")
def project_detail(project_id):
    with database() as db:
        admin(db)
        db.execute("BEGIN IMMEDIATE")
        project_data = read_project(db, project_id)
        now = time.time()
        expire(db, now)
        project_data["reserved_nodes"] = reservation_state(db, now, project_id)[project_id]
        project_data["tasks"] = {row["status"]: row["total"] for row in db.execute("""
            SELECT t.status,count(*) AS total FROM jobs j JOIN tasks t ON t.job=j.id
            WHERE j.project=? GROUP BY t.status
        """, (project_id,))}
        project_data["jobs"] = [dict(row) for row in db.execute("""
            WITH recent AS (SELECT id,created FROM jobs WHERE project=? ORDER BY created DESC,id DESC LIMIT 20)
            SELECT j.id,j.created,count(t.position) AS total,
                sum(t.status='done') AS completed,
                CASE WHEN EXISTS (SELECT 1 FROM job_cancellations WHERE job=j.id) THEN 'cancelled'
                     WHEN max(t.status='failed') THEN 'failed'
                     WHEN min(t.status='done') THEN 'done'
                     WHEN min(t.status='queued') THEN 'queued'
                     ELSE 'running' END AS status
            FROM recent j JOIN tasks t ON t.job=j.id GROUP BY j.id ORDER BY j.created DESC,j.id DESC
        """, (project_id,))]
        return jsonify(project_data)


@app.post("/admin/projects/<int:project_id>/metadata")
def update_project(project_id):
    values = project_fields(payload())
    with database() as db:
        admin(db)
        db.execute("BEGIN IMMEDIATE")
        current = read_project(db, project_id)
        if values != tuple(current[key] for key in ("name", "description", "affiliation", "contact")):
            db.execute("UPDATE projects SET name=?,description=?,affiliation=?,contact=? WHERE id=?", (*values, project_id))
            record_history(db, "project_updated", projects=(project_id,))
    return jsonify(saved=True)


@app.post("/admin/projects/<int:project_id>/rotate-key")
def rotate_project_key(project_id):
    payload()
    with database() as db:
        admin(db)
        db.execute("BEGIN IMMEDIATE")
        if read_project(db, project_id)["status"] != "approved":
            abort(409, description="Seuls les projets approuvés peuvent recevoir une clé API.")
        key = token()
        db.execute("UPDATE projects SET api_key=? WHERE id=?", (digest(key), project_id))
        record_history(db, "key_rotated", projects=(project_id,))
    return jsonify(api_key=key)


def reservation_state(db, now, project_id=None):
    reservations = defaultdict(list)
    for row in db.execute("""
        SELECT pn.project,n.id,n.name,n.device,n.hardware,
            (n.enabled=1 AND n.paused=0 AND coalesce(n.seen>=?,0) AND n.capsule IS ?) AS ready
        FROM project_nodes pn JOIN nodes n ON n.id=pn.node
        WHERE (? IS NULL OR pn.project=?) ORDER BY n.id
    """, (now - ONLINE_SECONDS, CAPSULE, project_id, project_id)):
        item = dict(row)
        reservations[item.pop("project")].append(item)
    return reservations


@app.get("/admin/state")
def state():
    with database() as db:
        admin(db)
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        expire(db, now)
        overview = dict(
            now=now,
            tasks={row["status"]: row["total"] for row in db.execute("""
                SELECT status, count(*) AS total FROM tasks GROUP BY status
            """)}
        )
        if request.args.get("include_projects") != "0":
            reservations = reservation_state(db, now)
            overview["projects"] = [dict(row, reserved_nodes=reservations[row["id"]]) for row in db.execute("""
                SELECT id,name,description,status,affiliation,contact FROM projects ORDER BY id DESC
            """)]
        if request.args.get("include_nodes") != "0":
            overview["nodes"] = [
                dict(row) for row in db.execute("SELECT id,name,seen,device,enabled FROM nodes ORDER BY id DESC")
            ]
        return jsonify(overview)


@app.get("/admin/fleet")
def fleet():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    page = request.args.get("page", 1, type=int)
    size = request.args.get("size", PAGE_SIZES[0], type=int)
    if len(search) > 160 or status not in ("", *NODE_STATES) or not 1 <= page <= 1_000_000 or size not in PAGE_SIZES:
        abort(400, description="Paramètres de recherche ou de pagination invalides.")
    now = time.time()
    classified = """
        WITH fleet AS (
            SELECT n.id,n.name,n.seen,n.device,n.hardware,p.name AS project_name,
                net.upload_limit_mbps,net.upload_mbps,net.download_mbps,net.rtt_ms,net.sampled_at,net.local_link, CASE
                WHEN n.enabled=0 THEN 'disabled'
                WHEN EXISTS (
                    SELECT 1 FROM tasks WHERE node=n.id AND status='running' AND expires>?
                ) THEN 'working'
                WHEN n.seen IS NULL AND n.device IS NULL THEN 'new'
                WHEN n.seen>=? AND n.paused=1 THEN 'paused'
                WHEN n.seen>=? AND pn.project IS NOT NULL THEN 'reserved'
                WHEN n.seen>=? THEN 'online'
                ELSE 'offline'
            END AS state FROM nodes n
            LEFT JOIN project_nodes pn ON pn.node=n.id LEFT JOIN projects p ON p.id=pn.project
            LEFT JOIN node_network net ON net.node=n.id
        )
    """
    matching = " WHERE (?='' OR state=?)"
    params = [now, *([now - ONLINE_SECONDS] * 3), status, status]
    with database() as db:
        admin(db)
        db.execute("BEGIN")
        if search:
            db.create_function("fold", 1, str.casefold, deterministic=True)
            matching += """ AND (instr(fold(name),?)>0 OR instr(fold(coalesce(device,'')),?)>0
                OR instr(fold(coalesce(hardware,'')),?)>0 OR instr(fold(coalesce(project_name,'')),?)>0
                OR cast(id as text)=?)"""
            params.extend([search.casefold()] * 4 + [search])
        counts = {item["state"]: item["total"] for item in db.execute(
            classified + "SELECT state,count(*) AS total FROM fleet GROUP BY state", params[:4]
        )}
        total = db.execute(classified + "SELECT count(*) FROM fleet" + matching, params).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        nodes = [dict(row) for row in db.execute(
            classified + "SELECT * FROM fleet" + matching + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        )]
        events = [dict(id=row["id"], created=row["created"], action=row["action"],
                       node_ids=[item["id"] for item in row["nodes"]], released=row["released"])
                  for row in history_entries(db, db.execute("""
                      SELECT * FROM history_events WHERE action IN ('enable','disable') ORDER BY id DESC LIMIT 8
                  """))]
    return jsonify(nodes=nodes, counts=counts, total=total, page=page, pages=pages, events=events,
                   now=now, sample_valid_seconds=SAMPLE_VALID_SECONDS)


@app.post("/admin/projects/<int:project_id>")
def manage_project(project_id):
    action = payload().get("action")
    if action not in ("approve", "reject"):
        abort(400)
    approved = action == "approve"
    key = token() if approved else None
    with database() as db:
        admin(db)
        status = "approved" if approved else "rejected"
        updated = db.execute("UPDATE projects SET status=?, api_key=? WHERE id=? AND status='pending'", (
            status, digest(key) if key else None, project_id
        ))
        if updated.rowcount != 1:
            abort(409, description="Ce projet a déjà été traité ou est introuvable.")
        record_history(db, "project_approved" if approved else "project_rejected", projects=(project_id,))
    return jsonify(status=status, api_key=key)


@app.post("/admin/projects/<int:project_id>/reservation")
def reserve_project(project_id):
    data = payload()
    count = data.get("count")
    ids = data.get("node_ids")
    if type(count) is not int or not 0 <= count <= MAX_BULK_NODES:
        abort(400, description=f"Choisissez un nombre de nœuds entre 0 et {MAX_BULK_NODES}.")
    if ids is not None and (
        not isinstance(ids, list) or len(ids) != count or
        any(type(node_id) is not int or not 1 <= node_id <= 2**63 - 1 for node_id in ids) or
        len(set(ids)) != count
    ):
        abort(400, description="La sélection doit contenir exactement le nombre de nœuds distincts demandé.")
    with database() as db:
        admin(db)
        db.execute("BEGIN IMMEDIATE")
        if project_id > 2**63 - 1 or not db.execute(
            "SELECT 1 FROM projects WHERE id=? AND status='approved'", (project_id,)
        ).fetchone():
            abort(404, description="Projet approuvé introuvable.")
        previous_ids = {row["node"] for row in db.execute("SELECT node FROM project_nodes WHERE project=?", (project_id,))}
        chosen = []
        if count:
            now = time.time()
            expire(db, now)
            candidates = db.execute("""
                SELECT n.id,n.name,n.device,n.hardware,pn.project FROM nodes n
                LEFT JOIN project_nodes pn ON pn.node=n.id
                WHERE n.enabled=1 AND n.paused=0 AND n.seen>=? AND n.capsule=?
                  AND (pn.project IS NULL OR pn.project=?)
                  AND NOT EXISTS (SELECT 1 FROM tasks WHERE node=n.id AND status='running')
                ORDER BY (pn.project IS NOT NULL) DESC, n.id
            """, (now - ONLINE_SECONDS, CAPSULE, project_id)).fetchall()
            if ids is not None:
                available = {row["id"]: row for row in candidates}
                if any(node_id not in available for node_id in ids):
                    abort(409, description="La sélection contient un nœud indisponible ou réservé à un autre projet.")
                chosen = [available[node_id] for node_id in ids]
            else:
                if len(candidates) < count:
                    abort(409, description=f"{len(candidates)} nœud(s) éligible(s) pour {count} demandé(s).")
                groups = defaultdict(list)
                unknown = []
                for row in candidates:
                    if row["hardware"]:
                        groups[row["hardware"].casefold()].append(row)
                    else:
                        unknown.append(row)
                sorted_groups = sorted(groups.values(), key=lambda group: (
                    -min(len(group), count), -sum(row["project"] is not None for row in group), group[0]["id"]
                ))
                chosen = [row for group in sorted_groups for row in group][:count]
                chosen.extend(unknown[:count - len(chosen)])
        next_ids = {row["id"] for row in chosen}
        if previous_ids != next_ids:
            db.execute("DELETE FROM project_nodes WHERE project=?", (project_id,))
            db.executemany("INSERT INTO project_nodes(node,project) VALUES (?,?)",
                           ((node_id, project_id) for node_id in sorted(next_ids)))
            record_history(db, "nodes_reserved" if count else "reservation_released",
                           nodes=previous_ids | next_ids, projects=(project_id,), quantity=count or len(previous_ids))
    profiles = {row["hardware"].casefold() for row in chosen if row["hardware"]}
    return jsonify(nodes=[{key: row[key] for key in ("id", "name", "device", "hardware")} for row in chosen],
                   homogeneous=bool(chosen) and len(profiles) == 1 and all(row["hardware"] for row in chosen))


@app.post("/admin/nodes")
def add_node():
    name = field(payload(), "name", 100)
    key = token()
    with database() as db:
        admin(db)
        if PILOT:
            abort(404)
        cursor = db.execute("INSERT INTO nodes(name,secret) VALUES (?,?)", (name, digest(key)))
        record_history(db, "node_created", nodes=(cursor.lastrowid,), quantity=1)
        return jsonify(id=cursor.lastrowid, node_token=key), 201


@app.post("/admin/enrollment-invites")
def create_invite():
    data = payload()
    label = field(data, "label", 100)
    hours = data.get("hours")
    maximum = data.get("max_uses")
    if type(hours) is not int or not 1 <= hours <= 168 or type(maximum) is not int or not 1 <= maximum <= 100:
        abort(400, description="Indiquez une durée de 1 à 168 heures et 1 à 100 inscriptions.")
    key = token()
    expires = time.time() + hours * 3600
    with database() as db:
        admin(db)
        invite = db.execute("INSERT INTO enrollment_invites(label,secret,expires,max_uses) VALUES (?,?,?,?)",
                            (label, digest(key), expires, maximum)).lastrowid
    return jsonify(id=invite, enrollment_key=key, expires=expires), 201


@app.get("/admin/enrollment-invites")
def list_invites():
    with database() as db:
        admin(db)
        return jsonify(invites=[dict(row) for row in db.execute("""
            SELECT id,label,expires,max_uses,uses,revoked FROM enrollment_invites ORDER BY id DESC LIMIT 100
        """)])


@app.post("/admin/enrollment-invites/<int:invite_id>/revoke")
def revoke_invite(invite_id):
    payload()
    with database() as db:
        admin(db)
        if invite_id > 2**63 - 1 or not db.execute(
            "UPDATE enrollment_invites SET revoked=1 WHERE id=?", (invite_id,)
        ).rowcount:
            abort(404)
    return jsonify(revoked=True)


@app.post("/v1/node/enroll")
def enroll_node():
    supplied = bearer()
    data = payload()
    name = field(data, "name", 100)
    identity = data.get("identity")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", identity):
        abort(400, description="Identité de poste invalide.")
    secret = digest(identity)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        invite = db.execute("SELECT id,expires,uses,max_uses,revoked FROM enrollment_invites WHERE secret=?",
                            (supplied,)).fetchone()
        if invite is None:
            if PILOT or not secrets.compare_digest(supplied, digest(
                (RUNTIME / "enrollment.key").read_text(encoding="utf-8").strip()
            )):
                abort(401, description="Clé d’inscription refusée.")
        else:
            registered = db.execute("""
                SELECT n.id FROM nodes n JOIN invite_nodes i ON i.node=n.id WHERE n.secret=? AND i.invite=?
            """, (secret, invite["id"])).fetchone()
            if registered is not None:
                return jsonify(id=registered["id"])
            if invite["revoked"] or invite["expires"] <= time.time() or invite["uses"] >= invite["max_uses"]:
                abort(401, description="Invitation indisponible.")
        created = db.execute("INSERT INTO nodes(name,secret) VALUES (?,?) ON CONFLICT(secret) DO NOTHING", (name, secret))
        row = db.execute("SELECT id FROM nodes WHERE secret=?", (secret,)).fetchone()
        if created.rowcount:
            if invite is not None:
                db.execute("INSERT INTO invite_nodes VALUES (?,?)", (row["id"], invite["id"]))
                db.execute("UPDATE enrollment_invites SET uses=uses+1 WHERE id=?", (invite["id"],))
            record_history(db, "node_enrolled", actor="agent", nodes=(row["id"],), quantity=1)
    return jsonify(id=row["id"])


@app.post("/admin/nodes/<int:node_id>/<any(enable,disable):action>")
def manage_node(node_id, action):
    payload()
    with database() as db:
        admin(db)
        change_nodes(db, [node_id], action)
    return jsonify(enabled=action == "enable")


@app.post("/admin/fleet/actions")
def fleet_action():
    data = payload()
    action = data.get("action")
    ids = data.get("node_ids")
    if action not in ("enable", "disable") or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BULK_NODES or any(
        type(node_id) is not int or not 1 <= node_id <= 2**63 - 1 for node_id in ids
    ):
        abort(400, description=f"Sélectionnez de 1 à {MAX_BULK_NODES} nœuds.")
    with database() as db:
        admin(db)
        changed, released = change_nodes(db, sorted(set(ids)), action)
    return jsonify(changed=changed, released=released)


def change_nodes(db, ids, action):
    if any(node_id > 2**63 - 1 for node_id in ids):
        abort(404, description="Nœud introuvable.")
    db.execute("BEGIN IMMEDIATE")
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(f"SELECT id,enabled FROM nodes WHERE id IN ({placeholders})", ids).fetchall()
    if len(rows) != len(ids):
        abort(404, description="Au moins un nœud de la sélection est introuvable.")
    enabled = action == "enable"
    changed = sorted(row["id"] for row in rows if bool(row["enabled"]) != enabled)
    if not changed:
        return 0, 0
    placeholders = ",".join("?" for _ in changed)
    now = time.time()
    if not enabled:
        expire(db, now)
    projects = [row["project"] for row in db.execute(f"""
        SELECT project FROM project_nodes WHERE node IN ({placeholders})
        UNION
        SELECT j.project FROM tasks t JOIN jobs j ON j.id=t.job
        WHERE t.status='running' AND t.node IN ({placeholders})
    """, (*changed, *changed))]
    db.execute(f"UPDATE nodes SET enabled=?, seen=NULL WHERE id IN ({placeholders})", (enabled, *changed))
    released = 0
    if not enabled:
        released = db.execute(f"""
            UPDATE tasks SET status='queued', node=NULL, lease=NULL, expires=NULL, attempts=attempts-1
            WHERE node IN ({placeholders}) AND status='running'
        """, changed).rowcount
    record_history(db, action, nodes=changed, projects=projects, quantity=len(changed), released=released, created=now)
    return len(changed), released


def batch_width(db, project_id, values, now):
    profiles = db.execute("""
        SELECT net.* FROM nodes n
        LEFT JOIN project_nodes pn ON pn.node=n.id LEFT JOIN node_network net ON net.node=n.id
        WHERE n.enabled=1 AND n.paused=0 AND n.seen>=? AND n.capsule=? AND (
            pn.project=? OR (pn.project IS NULL AND NOT EXISTS (
                SELECT 1 FROM project_nodes WHERE project=?
            ))
        )
    """, (now - ONLINE_SECONDS, CAPSULE, project_id, project_id)).fetchall()
    if not profiles:
        return WIDTH
    links = {transfer_profile(row, now) for row in profiles}
    per_value = max(len(json.dumps(value)) for value in values) + 2
    for width in range(WIDTH, 1, -1):
        if any(fits_lease(link, width * per_value + 2, width, LEASE_SECONDS) for link in links):
            return width
    return 1


@app.post("/v1/jobs")
def submit_job():
    data = payload()
    values = data.get("values")
    if data.get("capsule") != CAPSULE or not valid_values(values):
        abort(400, description=f"Fournir la capsule {CAPSULE} et 1 à {MAX_VALUES} valeurs entre -200 et 200.")
    request_key = request.headers.get("Idempotency-Key")
    if request_key is not None and not re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", request_key):
        abort(400, description="Clé d’idempotence invalide.")
    fingerprint = digest(json.dumps([CAPSULE, values], separators=(",", ":")))
    job_id = secrets.token_hex(16)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        project_id = project(db)
        if request_key is not None:
            known = db.execute("SELECT fingerprint,job FROM job_requests WHERE project=? AND request_key=?",
                               (project_id, request_key)).fetchone()
            if known is not None:
                if not secrets.compare_digest(known["fingerprint"], fingerprint):
                    abort(409, description="Cette clé est déjà associée à une autre demande.")
                response = jsonify(id=known["job"], status="queued")
                response.headers["Idempotency-Replayed"] = "true"
                return response, 202
        now = time.time()
        if PILOT:
            recent = db.execute("SELECT count(*) FROM jobs WHERE project=? AND created>=?",
                                (project_id, now - 60)).fetchone()[0]
            active = db.execute("""
                SELECT count(*) FROM jobs j WHERE j.project=? AND EXISTS (
                    SELECT 1 FROM tasks t WHERE t.job=j.id AND t.status IN ('queued','running')
                )
            """, (project_id,)).fetchone()[0]
            if recent >= PILOT_JOBS_PER_MINUTE or active >= PILOT_ACTIVE_JOBS:
                return jsonify(error="Quota de calcul du projet atteint."), 429, {"Retry-After": "60"}
        width = batch_width(db, project_id, values, now)
        db.execute("INSERT INTO jobs VALUES (?,?,?)", (job_id, project_id, now))
        if request_key is not None:
            db.execute("INSERT INTO job_requests VALUES (?,?,?,?)", (project_id, request_key, fingerprint, job_id))
        db.executemany("INSERT INTO tasks(job,position,input) VALUES (?,?,?)", (
            (job_id, index // width, json.dumps(values[index:index + width]))
            for index in range(0, len(values), width)
        ))
    return jsonify(id=job_id, status="queued"), 202


def expire(db, now):
    expired = db.execute("""
        UPDATE tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,
            node=NULL, lease=NULL, expires=NULL
        WHERE status='running' AND expires<=?
        RETURNING status
    """, (now,)).fetchall()
    if any(task["status"] == "failed" for task in expired):
        cancel_remaining(db)


def cancel_remaining(db):
    db.execute("""
        UPDATE tasks SET status='cancelled', node=NULL, lease=NULL, expires=NULL
        WHERE status IN ('queued','running') AND job IN (SELECT job FROM tasks WHERE status='failed')
    """)


@app.get("/v1/jobs/<job_id>")
def get_job(job_id):
    with database() as db:
        project_id = project(db)
        if not db.execute("SELECT 1 FROM jobs WHERE id=? AND project=?", (job_id, project_id)).fetchone():
            abort(404)
        expire(db, time.time())
        cancelled = db.execute("SELECT 1 FROM job_cancellations WHERE job=?", (job_id,)).fetchone() is not None
        tasks = db.execute("SELECT status,output FROM tasks WHERE job=? ORDER BY position", (job_id,)).fetchall()
    states = {task["status"] for task in tasks}
    progress = sum(task["status"] == "done" for task in tasks)
    if states == {"done"}:
        return jsonify(id=job_id, status="done", values=[
            value for task in tasks for value in json.loads(task["output"])
        ])
    status = "cancelled" if cancelled else "failed" if "failed" in states else "running" if states != {"queued"} else "queued"
    return jsonify(id=job_id, status=status, completed=progress, total=len(tasks))


@app.post("/v1/jobs/<job_id>/cancel")
def cancel_job(job_id):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        project_id = project(db)
        if not db.execute("SELECT 1 FROM jobs WHERE id=? AND project=?", (job_id, project_id)).fetchone():
            abort(404)
        if db.execute("SELECT 1 FROM job_cancellations WHERE job=?", (job_id,)).fetchone():
            return jsonify(id=job_id, status="cancelled")
        now = time.time()
        expire(db, now)
        changed = db.execute("""
            UPDATE tasks SET status='cancelled',node=NULL,lease=NULL,expires=NULL
            WHERE job=? AND status IN ('queued','running')
        """, (job_id,)).rowcount
        if not changed:
            abort(409, description="Cette demande est déjà terminée ou en échec.")
        db.execute("INSERT INTO job_cancellations VALUES (?,?)", (job_id, now))
    return jsonify(id=job_id, status="cancelled")


@app.route("/v1/node/network-probe", methods=["HEAD", "GET", "POST"])
def network_probe():
    if request.method == "POST" and request.content_length != PROBE_BYTES:
        abort(400, description="Taille de sonde invalide.")
    with database() as db:
        if request.method != "HEAD":
            db.execute("BEGIN IMMEDIATE")
        node_id = node(db)
        if request.method == "HEAD":
            return "", 204
        column = "last_upload_probe" if request.method == "POST" else "last_download_probe"
        info = db.execute(f"SELECT {column} FROM node_network WHERE node=?", (node_id,)).fetchone()
        now = time.time()
        if info and info[0] is not None and now - info[0] < PROBE_INTERVAL:
            return jsonify(error="Mesure récente."), 429, {"Retry-After": str(PROBE_INTERVAL)}
        if request.method == "POST" and len(request.get_data(cache=False)) != PROBE_BYTES:
            abort(400, description="Sonde incomplète.")
        db.execute(f"""
            INSERT INTO node_network(node,{column}) VALUES (?,?)
            ON CONFLICT(node) DO UPDATE SET {column}=excluded.{column}
        """, (node_id, now))
    if request.method == "POST":
        return "", 204
    return app.response_class(PROBE_BODY, mimetype="application/octet-stream")


def update_network(db, node_id, now, values):
    if not isinstance(values, dict):
        abort(400, description="Profil réseau invalide.")
    sample = values.get("sample")
    try:
        limit = optional_number(values.get("upload_limit_mbps"), MIN_UPLOAD_LIMIT, MAX_RATE)
        if sample is not None:
            if not isinstance(sample, dict) or type(sample.get("local_link")) is not bool:
                raise ValueError("Mesure réseau invalide.")
            local = sample["local_link"]
            delay = optional_number(sample.get("rtt_ms"), 0.01, 120_000)
            if delay is None:
                raise ValueError("La latence est requise.")
            upload = optional_number(sample.get("upload_mbps"), 0.001, MAX_RATE) if not local else None
            download = optional_number(sample.get("download_mbps"), 0.001, MAX_RATE) if not local else None
    except ValueError:
        abort(400, description="Mesure réseau invalide.")
    db.execute("""
        INSERT INTO node_network(node,upload_limit_mbps) VALUES (?,?)
        ON CONFLICT(node) DO UPDATE SET upload_limit_mbps=excluded.upload_limit_mbps,upload_mbps=NULL
        WHERE upload_limit_mbps IS NOT excluded.upload_limit_mbps
    """, (node_id, limit))
    if sample is not None:
        db.execute("""
            UPDATE node_network SET upload_mbps=?,download_mbps=?,rtt_ms=?,local_link=?,sampled_at=? WHERE node=?
        """, (upload, download, delay, local, now, node_id))


@app.post("/v1/node/claim")
def claim():
    data = payload()
    device = field(data, "device", 160)
    hardware = field(data, "hardware", 160) if "hardware" in data else None
    supported = data.get("capsule") == CAPSULE
    paused = data.get("paused", False)
    if type(paused) is not bool:
        abort(400, description="État du partage invalide.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        node_id = node(db)
        now = time.time()
        db.execute("UPDATE nodes SET seen=?,device=?,hardware=?,capsule=?,paused=? WHERE id=?",
                   (now, device, hardware, CAPSULE if supported else None, paused, node_id))
        if "network" in data:
            update_network(db, node_id, now, data["network"])
        expire(db, now)
        if paused or not supported or db.execute(
            "SELECT 1 FROM tasks WHERE node=? AND status='running'", (node_id,)
        ).fetchone():
            return jsonify(task=None)
        reservation = db.execute("SELECT project FROM project_nodes WHERE node=?", (node_id,)).fetchone()
        project_id = reservation["project"] if reservation is not None else None
        profile = transfer_profile(db.execute("SELECT * FROM node_network WHERE node=?", (node_id,)).fetchone(), now)
        db.create_function("fits_network_budget", 2, lambda size, length: int(
            fits_lease(profile, size, length, LEASE_SECONDS)
        ), deterministic=True)
        task = db.execute("""
            SELECT t.job,t.position,t.input FROM tasks t JOIN jobs j ON t.job=j.id
            WHERE t.status='queued' AND (
                j.project=? OR (? IS NULL AND NOT EXISTS (
                    SELECT 1 FROM project_nodes WHERE project=j.project
                ))
            ) AND fits_network_budget(length(t.input), json_array_length(t.input))
            ORDER BY j.created,t.position LIMIT 1
        """, (project_id, project_id)).fetchone()
        if task is None:
            return jsonify(task=None)
        lease = token()
        db.execute("""
            UPDATE tasks SET status='running',node=?,lease=?,expires=?,attempts=attempts+1
            WHERE job=? AND position=?
        """, (node_id, digest(lease), now + LEASE_SECONDS, task["job"], task["position"]))
        return jsonify(task=dict(job=task["job"], position=task["position"], lease=lease,
                                 capsule=CAPSULE, values=json.loads(task["input"])))


@app.post("/v1/node/result")
def result():
    data = payload()
    job = field(data, "job", 32)
    lease = digest(field(data, "lease", 128))
    position = data.get("position")
    if type(position) is not int or not 0 <= position < MAX_VALUES:
        abort(400)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        node_id = node(db)
        task = db.execute("""
            SELECT input,status,expires,attempts FROM tasks WHERE job=? AND position=? AND node=? AND lease=?
        """, (job, position, node_id, lease)).fetchone()
        if task is None:
            abort(409)
        if task["status"] == "done":
            return jsonify(status="done")
        if task["status"] != "running" or task["expires"] <= time.time():
            abort(409)
        values = data.get("values")
        inputs = json.loads(task["input"])
        if not isinstance(values, list) or len(values) != len(inputs) or any(
            type(value) not in (int, float) or not 0 <= value <= 40000 for value in values
        ):
            abort(400, description="Résultat invalide.")
        if PILOT and values != [fp16(fp16(value) ** 2) for value in inputs]:
            status = "failed" if task["attempts"] >= 3 else "queued"
            db.execute("""
                UPDATE tasks SET status=?,node=NULL,lease=NULL,expires=NULL WHERE job=? AND position=?
            """, (status, job, position))
            if status == "failed":
                cancel_remaining(db)
            app.logger.warning("Résultat divergent refusé job=%s lot=%s node=%s", job, position, node_id)
            return jsonify(error="Résultat divergent de la référence FP16."), 409
        db.execute("UPDATE tasks SET status='done',output=?,expires=NULL WHERE job=? AND position=?",
                   (json.dumps(values), job, position))
    return jsonify(status="done")


def fp16(value):
    return struct.unpack("<e", struct.pack("<e", value))[0]


if __name__ == "__main__":
    initialize()
    serve(app, host=os.environ.get("OPEN_NPU_BIND", "127.0.0.1") if PILOT else "127.0.0.1",
          port=int(os.environ.get("OPEN_NPU_PORT", "8787")), max_request_body_size=app.config["MAX_CONTENT_LENGTH"])
