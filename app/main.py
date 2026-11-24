"""Deploy-Automatik – Weboberfläche."""
import os
import re
import secrets
import time
from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, runner
from .clients import ApiError, GitHub, Portainer, ProxyManager, next_version
from .generator import TEMPLATES, read_zip
from .version import APP_VERSION

db.init()
app = Flask(__name__)
app.secret_key = db.get_setting("flask_secret") or secrets.token_hex(32)
db.set_setting("flask_secret", app.secret_key)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

STATUS_ICON = {"offen": "○", "läuft": "◐", "erledigt": "✓", "übersprungen": "–", "wartet": "…", "fehler": "✗"}


@app.context_processor
def inject():
    return {"STATUS_ICON": STATUS_ICON, "TEMPLATES": {**TEMPLATES, "self": "Deploy-Automatik selbst"},
            "APP_VERSION": APP_VERSION}


# ------------------------------------------------------------------ Anmeldung
def _password_hash():
    env = os.environ.get("ADMIN_PASSWORD", "")
    if env:
        return generate_password_hash(env, method="pbkdf2:sha256")
    return db.get_setting("admin_password_hash")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


@app.route("/anmelden", methods=["GET", "POST"])
def login():
    env_pw = os.environ.get("ADMIN_PASSWORD", "")
    stored = db.get_setting("admin_password_hash")
    first_run = not env_pw and not stored
    if request.method == "POST":
        pw = request.form.get("password", "")
        if first_run:
            if len(pw) < 8 or pw != request.form.get("password2"):
                flash("Passwort: mindestens 8 Zeichen, beide Eingaben gleich.", "fehler")
                return render_template("login.html", first_run=True)
            db.set_setting("admin_password_hash", generate_password_hash(pw))
            ok = True
        else:
            ok = (env_pw and secrets.compare_digest(pw, env_pw)) or (stored and check_password_hash(stored, pw))
        if ok:
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") else "/")
        flash("Passwort falsch.", "fehler")
    return render_template("login.html", first_run=first_run)


@app.get("/abmelden")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ Übersicht
@app.get("/")
@login_required
def index():
    items = []
    for p in db.projects():
        st = db.steps(p["id"])
        items.append({**p, "steps": st, "state": _state(p["id"], st)})
    ready = bool(db.get_setting("github_token")) and len(db.servers()) > 0
    return render_template("index.html", projects=items, ready=ready)


def _state(pid, steps):
    if runner.is_running(pid) or any(s["status"] == "läuft" for s in steps):
        return ("läuft", "Wird eingerichtet …")
    for s in steps:
        if s["status"] == "fehler":
            return ("fehler", f"Fehler: {s['title']}")
        if s["status"] == "wartet":
            return ("wartet", s["title"])
        if s["status"] == "offen":
            return ("offen", "Angehalten")
    return ("fertig", "Test und Produktiv eingerichtet")


# ------------------------------------------------------------- Neues Projekt
def _slugify(s):
    s = s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60]


def _parse_env(text):
    env = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Zeile „{line}“: erwartet NAME=Wert")
        k, v = line.split("=", 1)
        k = k.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise ValueError(f"„{k}“ ist kein gültiger Variablenname")
        if k in ("HOST_PORT", "APP_ENV", "APP_VERSION", "TZ"):
            raise ValueError(f"„{k}“ setzt die App selbst")
        env[k] = v.strip()
    return env


@app.route("/neu", methods=["GET", "POST"])
@login_required
def new_project():
    servers = db.servers()
    owner = db.get_setting("github_owner")
    if not servers or not db.get_setting("github_token"):
        flash("Bitte zuerst GitHub und mindestens einen Docker-Server einrichten.", "fehler")
        return redirect(url_for("settings"))
    f = request.form
    if request.method == "POST":
        errors = []
        name = f.get("name", "").strip()
        slug = _slugify(f.get("slug") or name)
        if not name or not slug:
            errors.append("Name fehlt.")
        try:
            cport = int(f.get("container_port") or 0)
            assert 1 <= cport <= 65535
        except (ValueError, AssertionError):
            errors.append("Container-Port ungültig.")
            cport = 0
        envs = {}
        for which, label in (("test", "Test"), ("prod", "Produktiv")):
            try:
                hport = int(f.get(f"{which}_port") or 0)
                assert 1 <= hport <= 65535
            except (ValueError, AssertionError):
                errors.append(f"{label}: Port ungültig.")
                hport = 0
            sid = int(f.get(f"{which}_server") or 0)
            srv = db.server(sid)
            if not srv:
                errors.append(f"{label}: Server wählen.")
            domain = f.get(f"{which}_domain", "").strip().lower()
            domain = re.sub(r"^https?://", "", domain).rstrip("/")
            if domain and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
                errors.append(f"{label}: Domain „{domain}“ ungültig.")
            try:
                env = _parse_env(f.get(f"{which}_env"))
            except ValueError as e:
                errors.append(f"{label}: {e}")
                env = {}
            envs[which] = {"server_id": sid, "server_name": srv["name"] if srv else "",
                           "host": srv["host"] if srv else "", "host_port": hport,
                           "domain": domain, "env": env, "volume_name": f"{slug}-{which}",
                           "data_path": _volume_location(srv, f"{slug}-{which}") if srv else ""}
        if f.get("test_server") and f.get("test_server") == f.get("prod_server") \
                and f.get("test_port") == f.get("prod_port"):
            errors.append("Test und Produktiv brauchen auf demselben Server verschiedene Ports.")
        volume = f.get("volume_path", "").strip()
        if volume and not volume.startswith("/"):
            errors.append("Datenordner im Container muss mit / beginnen.")
        if not volume:
            for e in envs.values():
                e.update(volume_name="", data_path="")
        upload_path = ""
        if f.get("template") == "zip":
            up = request.files.get("upload")
            if not up or not up.filename:
                errors.append("Bitte die ZIP mit dem Programm auswählen.")
            elif not errors:
                os.makedirs(os.path.join(db.DATA_DIR, "uploads"), exist_ok=True)
                upload_path = os.path.join(db.DATA_DIR, "uploads", f"{int(time.time())}-{slug}.zip")
                up.save(upload_path)
                try:
                    _, notes = read_zip(upload_path)
                except ValueError as e:
                    errors.append(str(e))
                    os.remove(upload_path)
        if errors:
            for e in errors:
                flash(e, "fehler")
            return render_template("new.html", servers=servers, owner=owner, f=f)
        data = {
            "name": name, "slug": slug, "owner": owner, "description": f.get("description", "").strip(),
            "private": f.get("visibility") == "private", "template": f.get("template", "python"),
            "container_port": cport, "volume_path": volume, "test": envs["test"], "prod": envs["prod"],
            "upload": upload_path,
        }
        pid = db.create_project(name, slug, data, runner.STEPS)
        db.add_log(pid, f"Projekt angelegt: {owner}/{slug}")
        if upload_path:
            db.add_log(pid, "ZIP: " + ", ".join(notes))
        runner.start(pid)
        return redirect(url_for("project", pid=pid))
    return render_template("new.html", servers=servers, owner=owner, f=f)


# ------------------------------------------------------------------ Projekt
@app.get("/projekt/<int:pid>")
@login_required
def project(pid):
    p = db.project(pid)
    if not p:
        return redirect("/")
    steps = db.steps(pid)
    links = _links(p)
    return render_template("project.html", p=p, d=p["data"], steps=steps, log=db.logs(pid),
                           state=_state(pid, steps), links=links)


def _links(p):
    d = p["data"]
    repo = f"https://github.com/{d['owner']}/{d['slug']}"
    links = {"repo": repo, "actions": f"{repo}/actions"}
    for w in ("test", "prod"):
        e = d.get(w)
        if e:
            links[w] = f"https://{e['domain']}" if e.get("domain") else f"http://{e['host']}:{e['host_port']}"
    if d.get("files_commit"):
        try:
            rel = GitHub(db.get_setting("github_token")).latest_release(d["owner"], d["slug"])
            tag = rel["tag_name"] if rel else None
        except Exception:  # noqa: BLE001
            tag = None
        links["current"] = tag
        links["next"] = next_version(tag)
        links["release"] = f"{repo}/releases/new?tag={links['next']}&target=main&title={links['next']}"
    return links


@app.post("/projekt/<int:pid>/weiter")
@login_required
def project_retry(pid):
    for s in db.steps(pid):
        if s["status"] in ("fehler", "wartet"):
            db.set_step(pid, s["key"], "offen", "")
            if s["key"] == "build":
                db.update_project_data(pid, build_wait_since=None)
    db.add_log(pid, "Fortsetzen angestoßen")
    runner.start(pid)
    return redirect(url_for("project", pid=pid))


@app.route("/projekt/<int:pid>/bearbeiten", methods=["GET", "POST"])
@login_required
def project_edit(pid):
    p = db.project(pid)
    if not p:
        return redirect("/")
    d = p["data"]
    is_self = d.get("kind") == "self"
    envs = [w for w in ("test", "prod") if d.get(w)]
    if request.method == "POST":
        f = request.form
        if runner.is_running(pid):
            flash("Die Einrichtung läuft gerade – bitte kurz warten.", "fehler")
            return render_template("edit.html", p=p, d=d, envs=envs, is_self=is_self, f=f)
        errors, changes, new = [], {}, {}
        keys_before = set().union(*[set(d[w].get("env") or {}) for w in envs])
        for w in envs:
            label = "Test" if w == "test" else "Produktiv"
            e = dict(d[w])
            dom = re.sub(r"^https?://", "", f.get(f"{w}_domain", "").strip().lower()).rstrip("/")
            if dom and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", dom):
                errors.append(f"{label}: Domain „{dom}“ ungültig.")
            port, env = e["host_port"], e.get("env") or {}
            if not is_self:
                try:
                    port = int(f.get(f"{w}_port") or 0)
                    assert 1 <= port <= 65535
                except (ValueError, AssertionError):
                    errors.append(f"{label}: Port ungültig.")
                    port = e["host_port"]
                try:
                    env = _parse_env(f.get(f"{w}_env"))
                except ValueError as ex:
                    errors.append(f"{label}: {ex}")
            ch = {"domain": dom != (e.get("domain") or ""), "port": port != int(e["host_port"]), "env": env != (e.get("env") or {})}
            if ch["port"]:
                srv = db.server(e["server_id"])
                try:
                    used = Portainer(srv["url"], srv["api_key"], srv["verify_tls"]).used_ports(srv["endpoint_id"])
                    owner = used.get(port)
                    if owner and owner != f"{d['slug']}-{w}":
                        errors.append(f"{label}: Port {port} auf {srv['name']} ist schon von „{owner}“ belegt.")
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{label}: Server nicht erreichbar ({ex}).")
            e.update(domain=dom, host_port=port, env=env)
            new[w] = e
            if any(ch.values()):
                changes[w] = ch
        if len(envs) == 2 and new["test"]["server_id"] == new["prod"]["server_id"] \
                and new["test"]["host_port"] == new["prod"]["host_port"]:
            errors.append("Test und Produktiv brauchen auf demselben Server verschiedene Ports.")
        if errors:
            for e in errors:
                flash(e, "fehler")
            return render_template("edit.html", p=p, d=d, envs=envs, is_self=is_self, f=f)
        if not changes:
            flash("Nichts geändert.", "ok")
            return redirect(url_for("project", pid=pid))
        keys_after = set().union(*[set(new[w].get("env") or {}) for w in envs])
        db.update_project_data(pid, **new)
        for w, ch in changes.items():
            label = "Test" if w == "test" else "Produktiv"
            was = [n for n, k in (("Domain", "domain"), ("Port", "port"), ("Umgebungsvariablen", "env")) if ch[k]]
            db.add_log(pid, f"{label}: geändert – {', '.join(was)}")
        runner.apply_settings(pid, changes, keys_after != keys_before)
        return redirect(url_for("project", pid=pid))
    return render_template("edit.html", p=p, d=d, envs=envs, is_self=is_self, f={})


def _env_text(env):
    return "\n".join(f"{k}={v}" for k, v in (env or {}).items())


app.jinja_env.filters["envtext"] = _env_text


@app.post("/projekt/<int:pid>/entfernen")
@login_required
def project_delete(pid):
    db.delete_project(pid)
    flash("Aus der Liste entfernt. Repository, Stacks und Proxy-Hosts bleiben bestehen.", "ok")
    return redirect("/")


@app.get("/api/projekt/<int:pid>")
@login_required
def project_api(pid):
    steps = db.steps(pid)
    return jsonify(state=_state(pid, steps)[0], steps=steps, log=db.logs(pid, 50))


# ------------------------------------------------------------- Einstellungen
@app.route("/einstellungen", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        section = request.form.get("section")
        f = request.form
        if section == "github":
            if f.get("github_token"):
                db.set_setting("github_token", f["github_token"].strip())
            db.set_setting("github_owner", f.get("github_owner", "").strip())
            try:
                gh = GitHub(db.get_setting("github_token"))
                login, scopes = gh.user()
                if not db.get_setting("github_owner"):
                    db.set_setting("github_owner", login)
                msg = f"GitHub verbunden als {login}."
                if scopes is not None:
                    missing = [s for s in ("repo", "workflow", "write:packages") if s not in scopes]
                    if missing:
                        msg += f" Dem Token fehlen Rechte: {', '.join(missing)}."
                flash(msg, "ok" if "fehlen" not in msg else "fehler")
            except Exception as e:  # noqa: BLE001
                flash(f"GitHub: {e}", "fehler")
        elif section == "npm":
            db.set_setting("npm_url", f.get("npm_url", "").strip().rstrip("/"))
            db.set_setting("npm_email", f.get("npm_email", "").strip())
            if f.get("npm_password"):
                db.set_setting("npm_password", f["npm_password"])
            db.set_setting("npm_certificate", f.get("npm_certificate", "new"))
            db.set_setting("npm_le_email", f.get("npm_le_email", "").strip())
            if db.get_setting("npm_url"):
                try:
                    n = len(ProxyManager(db.get_setting("npm_url"), db.get_setting("npm_email"),
                                         db.get_setting("npm_password")).certificates())
                    flash(f"Proxy Manager verbunden ({n} Zertifikate vorhanden).", "ok")
                except Exception as e:  # noqa: BLE001
                    flash(str(e), "fehler")
        return redirect(url_for("settings"))
    certs, cert_error = [], ""
    if db.get_setting("npm_url"):
        try:
            certs = ProxyManager(db.get_setting("npm_url"), db.get_setting("npm_email"),
                                 db.get_setting("npm_password")).certificates()
        except Exception as e:  # noqa: BLE001
            cert_error = str(e)
    s = {k: db.get_setting(k) for k in ("github_owner", "npm_url", "npm_email", "npm_certificate", "npm_le_email")}
    s["has_token"] = bool(db.get_setting("github_token"))
    s["has_npm_password"] = bool(db.get_setting("npm_password"))
    return render_template("settings.html", s=s, servers=db.servers(), certs=certs, cert_error=cert_error)


@app.route("/server/<int:sid>", methods=["GET", "POST"])
@app.route("/server/neu", methods=["GET", "POST"], defaults={"sid": 0})
@login_required
def server_edit(sid):
    srv = db.server(sid) if sid else None
    if request.method == "POST":
        f = request.form
        try:
            endpoint = int(f.get("endpoint_id") or 0)
        except ValueError:
            endpoint = 0
        name, url, host = f.get("name", "").strip(), f.get("url", "").strip().rstrip("/"), f.get("host", "").strip()
        key = f.get("api_key", "").strip()
        if not (name and url and host and endpoint and (key or srv)):
            flash("Bitte alle Felder ausfüllen und eine Umgebung wählen.", "fehler")
            return render_template("server.html", srv=srv, f=f)
        sid = db.save_server(sid, name, url, key, endpoint, host, f.get("verify_tls") == "1")
        flash(f"Server „{name}“ gespeichert.", "ok")
        return redirect(url_for("settings"))
    return render_template("server.html", srv=srv, f={})


@app.post("/server/<int:sid>/loeschen")
@login_required
def server_delete(sid):
    db.delete_server(sid)
    flash("Server entfernt.", "ok")
    return redirect(url_for("settings"))


@app.post("/api/portainer/umgebungen")
@login_required
def portainer_endpoints():
    j = request.get_json(force=True)
    key = j.get("api_key") or ""
    verify = bool(j.get("verify_tls", True))
    if not key and j.get("id"):
        srv = db.server(int(j["id"]))
        key = srv["api_key"] if srv else ""
    try:
        pt = Portainer(j.get("url", ""), key, verify)
        return jsonify(ok=True, version=pt.version(), endpoints=pt.endpoints())
    except (ApiError, Exception) as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e))


def _volume_location(srv, name):
    """Wo das Named Volume auf dem Server landet (Docker-Datenordner/volumes/<name>/_data)."""
    try:
        root = Portainer(srv["url"], srv["api_key"], srv["verify_tls"]).docker_root(srv["endpoint_id"])
    except Exception:  # noqa: BLE001
        return ""
    return f"{root.rstrip('/')}/volumes/{name}/_data" if root else ""


@app.get("/api/volume-ort")
@login_required
def volume_location():
    srv = db.server(int(request.args.get("server") or 0))
    name = request.args.get("name", "").strip()
    if not srv or not name:
        return jsonify(ok=False)
    loc = _volume_location(srv, name)
    if not loc:
        return jsonify(ok=True, hint=f"Volume „{name}“ – Docker-Datenordner von {srv['name']} nicht abfragbar.")
    warn = "" if loc.startswith("/docker/") else " – Achtung: Docker-Datenordner ist hier nicht /docker"
    return jsonify(ok=True, hint=f"Volume „{name}“ → {loc}{warn}")


# ---------------------------------------------------------- App selbst
@app.route("/selbst", methods=["GET", "POST"])
@login_required
def self_setup():
    existing = next((p for p in db.projects() if p["data"].get("kind") == "self"), None)
    if existing:
        return redirect(url_for("project", pid=existing["id"]))
    servers = db.servers()
    owner = db.get_setting("github_owner")
    if not servers or not db.get_setting("github_token"):
        flash("Bitte zuerst GitHub und den Docker-Server eintragen, auf dem diese App läuft.", "fehler")
        return redirect(url_for("settings"))
    f = request.form
    if request.method == "POST":
        errors = []
        srv = db.server(int(f.get("server") or 0))
        slug = re.sub(r"[^a-z0-9-]+", "-", f.get("slug", runner.SELF_NAME).lower()).strip("-") or runner.SELF_NAME
        try:
            port = int(f.get("port") or 0)
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            errors.append("Port ungültig.")
            port = 0
        if not srv:
            errors.append("Server wählen.")
        if errors:
            for e in errors:
                flash(e, "fehler")
            return render_template("self.html", servers=servers, owner=owner, f=f)
        data = {"kind": "self", "name": "Deploy-Automatik", "slug": slug, "owner": owner,
                "description": "Legt Apps mit GitHub-Pipeline, Test- und Produktivserver an",
                "private": f.get("visibility") == "private", "template": "self", "container_port": 8000,
                "volume_path": "/data", "test": None,
                "prod": {"server_id": srv["id"], "server_name": srv["name"], "host": srv["host"], "host_port": port,
                         "domain": "", "env": {}, "volume_name": runner.SELF_NAME,
                         "data_path": _volume_location(srv, runner.SELF_NAME)}}
        pid = db.create_project("Deploy-Automatik", slug, data, runner.SELF_STEPS)
        db.add_log(pid, f"Umstieg auf GitHub/Portainer angelegt: {owner}/{slug}")
        runner.start(pid)
        return redirect(url_for("project", pid=pid))
    return render_template("self.html", servers=servers, owner=owner, f=f)


@app.get("/gesund")
def health():
    return {"status": "ok", "version": APP_VERSION}


runner.resume()
runner.poller()
