"""Datenhaltung (SQLite im Volume /data) und Verschlüsselung der Zugangsdaten."""
import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "deploy-automatik.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    api_key TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL,
    host TEXT NOT NULL,
    verify_tls INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    data TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    project_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offen',
    message TEXT NOT NULL DEFAULT '',
    updated REAL,
    PRIMARY KEY (project_id, key)
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);
"""


@contextmanager
def connect():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- Schlüssel
def _fernet():
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        path = os.path.join(DATA_DIR, "secret.key")
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(base64.urlsafe_b64encode(os.urandom(32)).decode())
            os.chmod(path, 0o600)
        with open(path) as f:
            secret = f.read().strip()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


# ------------------------------------------------------------ Einstellungen
SECRET_SETTINGS = {"github_token", "npm_password"}


def get_setting(key, default=""):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return decrypt(row["value"]) if key in SECRET_SETTINGS else row["value"]


def set_setting(key, value):
    stored = encrypt(value) if key in SECRET_SETTINGS else value
    with connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stored))


# ----------------------------------------------------------------- Server
def servers():
    with connect() as c:
        rows = c.execute("SELECT * FROM servers ORDER BY name").fetchall()
    return [_server(r) for r in rows]


def server(sid):
    with connect() as c:
        r = c.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    return _server(r) if r else None


def _server(r):
    d = dict(r)
    d["api_key"] = decrypt(d["api_key"])
    d["verify_tls"] = bool(d["verify_tls"])
    return d


def save_server(sid, name, url, api_key, endpoint_id, host, verify_tls):
    with connect() as c:
        if sid:
            if api_key:
                c.execute("UPDATE servers SET name=?,url=?,api_key=?,endpoint_id=?,host=?,verify_tls=? WHERE id=?",
                          (name, url, encrypt(api_key), endpoint_id, host, int(verify_tls), sid))
            else:
                c.execute("UPDATE servers SET name=?,url=?,endpoint_id=?,host=?,verify_tls=? WHERE id=?",
                          (name, url, endpoint_id, host, int(verify_tls), sid))
            return sid
        cur = c.execute("INSERT INTO servers(name,url,api_key,endpoint_id,host,verify_tls) VALUES(?,?,?,?,?,?)",
                        (name, url, encrypt(api_key), endpoint_id, host, int(verify_tls)))
        return cur.lastrowid


def delete_server(sid):
    with connect() as c:
        c.execute("DELETE FROM servers WHERE id=?", (sid,))


# ---------------------------------------------------------------- Projekte
def create_project(name, slug, data, steps):
    with connect() as c:
        cur = c.execute("INSERT INTO projects(name,slug,data,created) VALUES(?,?,?,?)",
                        (name, slug, json.dumps(data), time.time()))
        pid = cur.lastrowid
        for i, (key, title) in enumerate(steps):
            c.execute("INSERT INTO steps(project_id,idx,key,title,status,updated) VALUES(?,?,?,?, 'offen', ?)",
                      (pid, i, key, title, time.time()))
    return pid


def projects():
    with connect() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY created DESC").fetchall()
    return [_project(r) for r in rows]


def project(pid):
    with connect() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return _project(r) if r else None


def _project(r):
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


def update_project_data(pid, **changes):
    with connect() as c:
        r = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        data = json.loads(r["data"])
        data.update(changes)
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(data), pid))
    return data


def delete_project(pid):
    with connect() as c:
        for t in ("projects", "steps", "log"):
            c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'projects' else 'project_id'}=?", (pid,))


def steps(pid):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM steps WHERE project_id=? ORDER BY idx", (pid,))]


def set_step(pid, key, status, message=""):
    with connect() as c:
        c.execute("UPDATE steps SET status=?, message=?, updated=? WHERE project_id=? AND key=?",
                  (status, message, time.time(), pid, key))


def add_log(pid, text):
    with connect() as c:
        c.execute("INSERT INTO log(project_id,ts,text) VALUES(?,?,?)", (pid, time.time(), text))


def logs(pid, limit=300):
    with connect() as c:
        rows = c.execute("SELECT * FROM log WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]
