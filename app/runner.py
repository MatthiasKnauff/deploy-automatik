"""Arbeitet die Einrichtungsschritte eines Projekts der Reihe nach ab."""
import json
import os
import threading
import time
import traceback

from . import db
from .clients import ApiError, GitHub, Portainer, ProxyManager
from .generator import compose_template, generate, generate_self

SELF_NAME = "deploy-automatik"
SOURCE_DIR = os.environ.get("SOURCE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STEPS = [
    ("check", "Vorabprüfung (Zugänge, Name, Ports, Domains)"),
    ("repo", "GitHub: Repository anlegen"),
    ("files", "GitHub: Startprogramm, Dockerfile und Pipeline hochladen"),
    ("actions", "GitHub: Schreibrechte für die Pipeline setzen"),
    ("build", "GitHub: ersten Build abwarten"),
    ("test_stack", "Testserver: Portainer-Stack anlegen"),
    ("test_proxy", "Testserver: Proxy-Host anlegen"),
    ("release", "Auf erste Freigabe (Release) warten"),
    ("prod_stack", "Produktivserver: Portainer-Stack anlegen"),
    ("prod_proxy", "Produktivserver: Proxy-Host anlegen"),
]

SELF_STEPS = [
    ("check", "Vorabprüfung (Zugänge, Server, Datenordner)"),
    ("repo", "GitHub: Repository anlegen"),
    ("files", "GitHub: eigenen Programmcode und Pipeline hochladen"),
    ("actions", "GitHub: Schreibrechte für die Pipeline setzen"),
    ("build", "GitHub: ersten Build abwarten"),
    ("self_release", "GitHub: laufende Version als erste Freigabe eintragen"),
    ("self_wait", "GitHub: Build der Freigabe abwarten"),
    ("self_switch", "Umstieg: Daten übernehmen und als Portainer-Stack neu starten"),
]

BUILD_TIMEOUT = 25 * 60


class Wait(Exception):
    """Schritt kann noch nicht abgeschlossen werden – später erneut versuchen."""


class Skip(Exception):
    pass


_running = set()
_running_lock = threading.Lock()


def is_running(pid):
    return pid in _running


def start(pid):
    with _running_lock:
        if pid in _running:
            return False
        _running.add(pid)
    threading.Thread(target=_run, args=(pid,), daemon=True).start()
    return True


def _run(pid):
    try:
        for st in db.steps(pid):
            if st["status"] in ("erledigt", "übersprungen"):
                continue
            db.set_step(pid, st["key"], "läuft")
            try:
                msg = STEP_FUNCS[st["key"]](Ctx(pid)) or ""
                db.set_step(pid, st["key"], "erledigt", msg)
                db.add_log(pid, f"✓ {st['title']}" + (f" – {msg}" if msg else ""))
            except Skip as e:
                db.set_step(pid, st["key"], "übersprungen", str(e))
                db.add_log(pid, f"– {st['title']}: {e}")
            except Wait as e:
                db.set_step(pid, st["key"], "wartet", str(e))
                return
            except ApiError as e:
                db.set_step(pid, st["key"], "fehler", str(e))
                db.add_log(pid, f"✗ {st['title']}: {e}")
                return
            except Exception as e:  # noqa: BLE001
                db.set_step(pid, st["key"], "fehler", f"{type(e).__name__}: {e}")
                db.add_log(pid, f"✗ {st['title']}: {traceback.format_exc(limit=3)}")
                return
    finally:
        with _running_lock:
            _running.discard(pid)


def poller(interval=60):
    """Projekte, die auf etwas warten (Build, Freigabe), regelmäßig weiterführen."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                for p in db.projects():
                    if any(s["status"] == "wartet" for s in db.steps(p["id"])):
                        start(p["id"])
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------ Kontext
class Ctx:
    def __init__(self, pid):
        self.pid = pid
        self.project = db.project(pid)
        self.p = self.project["data"]
        self.owner = self.p["owner"]
        self.slug = self.p["slug"]
        self._gh = None

    @property
    def gh(self):
        if self._gh is None:
            token = db.get_setting("github_token")
            if not token:
                raise ApiError("Kein GitHub-Token in den Einstellungen hinterlegt.")
            self._gh = GitHub(token)
        return self._gh

    def log(self, text):
        db.add_log(self.pid, text)

    def save(self, **kw):
        self.p = db.update_project_data(self.pid, **kw)

    def env(self, which):
        return dict(self.p[which]), db.server(self.p[which]["server_id"])

    def portainer(self, server):
        if not server:
            raise ApiError("Der gewählte Docker-Server existiert nicht mehr in den Einstellungen.")
        return Portainer(server["url"], server["api_key"], server["verify_tls"])

    def npm(self):
        url = db.get_setting("npm_url")
        if not url:
            raise ApiError("Kein Nginx Proxy Manager in den Einstellungen hinterlegt.")
        return ProxyManager(url, db.get_setting("npm_email"), db.get_setting("npm_password"))


# ------------------------------------------------------------------- Schritte
def _which(c):
    return [w for w in ("test", "prod") if c.p.get(w)]


def _stack_name(c, which):
    return SELF_NAME if c.p.get("kind") == "self" else f"{c.slug}-{which}"


def step_check(c):
    problems = []
    login, _ = c.gh.user()
    is_self = c.p.get("kind") == "self"
    if c.gh.repo(c.owner, c.slug):
        problems.append(f"Repository {c.owner}/{c.slug} gibt es schon.")
    for which in _which(c):
        label = "Testserver" if which == "test" else "Produktivserver"
        e, srv = c.env(which)
        try:
            pt = c.portainer(srv)
            ep = srv["endpoint_id"]
            if pt.find_stack(_stack_name(c, which), ep):
                problems.append(f"{label}: Stack {_stack_name(c, which)} gibt es schon.")
            used = pt.used_ports(ep)
            owner_of_port = used.get(int(e["host_port"]))
            if owner_of_port and not (is_self and owner_of_port == SELF_NAME):
                problems.append(f"{label} {srv['name']}: Port {e['host_port']} ist schon von „{owner_of_port}“ belegt.")
            if is_self and not pt.container(ep, SELF_NAME):
                problems.append(f"Auf {srv['name']} läuft kein Container „{SELF_NAME}“ – ist das der richtige Server?")
            if e.get("volume_name") and not is_self and pt.volume(ep, e["volume_name"]):
                problems.append(f"{label} {srv['name']}: Volume {e['volume_name']} gibt es schon – "
                                "es würden fremde Daten übernommen.")
        except ApiError as ex:
            problems.append(f"{label}: {ex}")
    if c.p.get("test") and c.p["test"]["server_id"] == c.p["prod"]["server_id"]:
        if int(c.p["test"]["host_port"]) == int(c.p["prod"]["host_port"]):
            problems.append("Test und Produktiv laufen auf demselben Server mit demselben Port.")
    domains = [c.p[w]["domain"] for w in _which(c) if c.p[w].get("domain")]
    if domains:
        try:
            npm = c.npm()
            for d in domains:
                if npm.find_host(d):
                    problems.append(f"Domain {d} ist im Proxy Manager schon vergeben.")
        except ApiError as ex:
            problems.append(str(ex))
    if problems:
        raise ApiError(" · ".join(problems))
    return f"angemeldet bei GitHub als {login}"


def step_repo(c):
    if c.p.get("repo_created"):
        raise Skip("schon angelegt")
    org = c.gh.is_org(c.owner)
    r = c.gh.create_repo(c.owner, c.slug, c.p.get("description", ""), c.p["private"], org)
    c.save(repo_created=True, repo_url=r["html_url"], clone_url=r["clone_url"])
    return r["html_url"]


def step_files(c):
    if c.p.get("files_commit"):
        raise Skip("schon hochgeladen")
    if c.p.get("kind") == "self":
        files = generate_self(SOURCE_DIR)
        sha = c.gh.commit_files(c.owner, c.slug, files, "Deploy-Automatik: Programmcode und Pipeline")
    else:
        files = generate(c.p, c.owner, c.p.get("upload"))
        msg = "Programm übernommen, Pipeline eingerichtet" if c.p["template"] == "zip" \
            else "Start: App-Gerüst, Dockerfile und Pipeline"
        sha = c.gh.commit_files(c.owner, c.slug, files, msg)
    c.save(files_commit=sha)
    return f"{len(files)} Dateien, Commit {sha[:7]}"


def step_actions(c):
    try:
        c.gh.set_workflow_permissions(c.owner, c.slug)
    except ApiError as e:
        c.log(f"Hinweis: Workflow-Rechte nicht gesetzt ({e}). Die Pipeline fordert sie selbst an.")
        raise Skip("nicht nötig – die Pipeline fordert ihre Rechte selbst an")
    return "Pipeline darf Image und Server-Zweige schreiben"


def step_build(c):
    started = c.p.get("build_wait_since")
    if not started:
        started = time.time()
        c.save(build_wait_since=started)
    while True:
        # Fertig, sobald die Pipeline den Testzweig geschrieben hat (auch nach einem späteren, reparierten Build)
        if c.gh.branch_exists(c.owner, c.slug, "deploy-test"):
            runs = c.gh.runs(c.owner, c.slug, per_page=1)
            return f"Build grün: {runs[0]['html_url']}" if runs else "Build grün"
        runs = c.gh.runs(c.owner, c.slug, per_page=1)
        run = runs[0] if runs else None
        if run and run["status"] == "completed" and run["conclusion"] != "success":
            raise ApiError(f"Build fehlgeschlagen ({run['conclusion']}): {run['html_url']} – "
                           "nach dem Reparieren „Erneut versuchen“ klicken.")
        if time.time() - started > BUILD_TIMEOUT:
            raise ApiError("Der erste Build hat nicht innerhalb von 25 Minuten geendet. "
                           f"Bitte unter {c.p['repo_url']}/actions nachsehen.")
        time.sleep(10)


def _registry(c, pt, srv):
    token = db.get_setting("github_token")
    login, _ = c.gh.user()
    try:
        _, new = pt.ensure_registry(login, token)
        if new:
            c.log(f"{srv['name']}: Registry ghcr.io hinterlegt")
    except ApiError as ex:
        c.log(f"Hinweis {srv['name']}: Registry ghcr.io nicht angelegt ({ex})")
    return login, token


def _stack_env(e):
    env = {"HOST_PORT": e["host_port"], **e.get("env", {})}
    if e.get("volume_name"):
        env["VOLUME_NAME"] = e["volume_name"]
    return env


def _stack(c, which):
    key = f"{which}_stack_id"
    if c.p.get(key):
        raise Skip("schon angelegt")
    e, srv = c.env(which)
    pt = c.portainer(srv)
    name = _stack_name(c, which)
    existing = pt.find_stack(name, srv["endpoint_id"])
    if existing:
        c.save(**{key: existing["Id"]})
        return f"Stack {name} war schon vorhanden (Id {existing['Id']})"
    login, token = _registry(c, pt, srv)
    auth = (login, token) if c.p["private"] else None
    st = pt.create_git_stack(srv["endpoint_id"], name, c.p["clone_url"], f"refs/heads/deploy-{which}",
                             _stack_env(e), auth)
    c.save(**{key: st.get("Id")})
    url = f"http://{srv['host']}:{e['host_port']}"
    data = f", Daten im Volume {e['volume_name']}" if e.get("volume_name") else ""
    return f"Stack {name} auf {srv['name']} läuft – {url}{data} (Portainer prüft jede Minute auf Neues)"


def _proxy(c, which):
    """Proxy-Host anlegen – auch nachträglich: neue Domain wird angelegt, die bisherige (von der App angelegte) entfernt."""
    e, srv = c.env(which)
    key_id, key_dom = f"{which}_proxy_id", f"{which}_proxy_domain"
    old_id, old_dom = c.p.get(key_id), c.p.get(key_dom)
    if old_id and old_dom == e.get("domain"):
        raise Skip("schon angelegt")
    if not e.get("domain") and not old_id:
        raise Skip("keine Domain angegeben")
    npm = c.npm()
    result = "Domain entfernt"
    if e.get("domain"):
        existing = npm.find_host(e["domain"])
        if existing:
            if existing.get("forward_host") != srv["host"] or int(existing.get("forward_port", 0)) != int(e["host_port"]):
                raise ApiError(f"{e['domain']} zeigt im Proxy Manager schon auf "
                               f"{existing.get('forward_host')}:{existing.get('forward_port')}.")
            new_id, result = existing["id"], f"{e['domain']} war schon richtig eingetragen"
        else:
            cert = db.get_setting("npm_certificate", "new")
            host, note = npm.create_host(e["domain"], srv["host"], e["host_port"], cert, db.get_setting("npm_le_email"))
            new_id, result = host.get("id"), f"{e['domain']} → {srv['host']}:{e['host_port']}"
            if note:
                c.log(note)
                result += " (ohne SSL, siehe Protokoll)"
    else:
        new_id = None
    if old_id and old_id != new_id:
        npm.delete_host(old_id)
        c.log(f"Bisherigen Proxy-Host {old_dom or old_id} entfernt")
    c.save(**{key_id: new_id, key_dom: e.get("domain") or None})
    return result


def step_release(c):
    if c.gh.branch_exists(c.owner, c.slug, "deploy-prod"):
        rel = c.gh.latest_release(c.owner, c.slug)
        return f"freigegeben: {rel['tag_name']}" if rel else "freigegeben"
    raise Wait("Sobald du in GitHub ein Release freigibst, richtet die App den Produktivserver ein "
               "(sie schaut jede Minute nach).")


# ------------------------------------------------------------- App selbst
SWITCH_SCRIPT = r"""
set -u
log() { echo "$(date '+%H:%M:%S') $*"; }
sleep 3
log "alte Instanz anhalten"
docker stop -t 20 deploy-automatik || { log "Anhalten fehlgeschlagen"; exit 1; }
mkdir -p "$ZIEL"
KOPIERT=0
if [ -z "$(ls -A "$ZIEL")" ]; then
  log "Daten ins neue Volume kopieren"
  KOPIERT=1
  cp -a /data/. "$ZIEL"/ || { log "Kopieren fehlgeschlagen"; find "$ZIEL" -mindepth 1 -delete; docker start deploy-automatik; exit 1; }
else
  log "neues Volume ist nicht leer – vorhandene Daten werden verwendet"
fi
docker rename deploy-automatik deploy-automatik-alt
printf '%s' "$BODY" > /tmp/body.json
for U in "$URL1" "$URL2"; do
  log "Portainer-Stack anlegen"
  if wget -q -O /tmp/antwort -T 900 $TLS --header "X-API-Key: $KEY" --header "Content-Type: application/json" \
       --post-file /tmp/body.json "$U"; then
    log "fertig – alte Instanz bleibt angehalten als deploy-automatik-alt"
    exit 0
  fi
done
log "Stack konnte nicht angelegt werden – alte Instanz wird wieder gestartet"
[ "$KOPIERT" = 1 ] && find "$ZIEL" -mindepth 1 -delete
docker rm -f deploy-automatik >/dev/null 2>&1
docker rename deploy-automatik-alt deploy-automatik
docker start deploy-automatik
exit 1
"""
HELPER_SWITCH = "deploy-automatik-umstieg"


def step_self_release(c):
    from .version import APP_VERSION
    rel = c.gh.latest_release(c.owner, c.slug)
    if rel:
        raise Skip(f"schon freigegeben: {rel['tag_name']}")
    tag = f"v{APP_VERSION}"
    c.gh.create_release(c.owner, c.slug, tag, "Stand, mit dem die Deploy-Automatik von Hand gestartet wurde.")
    return f"{tag} freigegeben (entspricht der laufenden Version)"


def step_self_wait(c):
    started = time.time()
    while True:
        if c.gh.branch_exists(c.owner, c.slug, "deploy-prod"):
            return "Produktiv-Image ist gebaut"
        runs = c.gh.runs(c.owner, c.slug, per_page=1)
        if runs and runs[0]["status"] == "completed" and runs[0]["conclusion"] != "success":
            raise ApiError(f"Build fehlgeschlagen ({runs[0]['conclusion']}): {runs[0]['html_url']}")
        if time.time() - started > BUILD_TIMEOUT:
            raise ApiError(f"Build der Freigabe dauert zu lange – {c.p['repo_url']}/actions ansehen.")
        time.sleep(10)


def step_self_switch(c):
    e, srv = c.env("prod")
    pt = c.portainer(srv)
    ep = srv["endpoint_id"]
    st = pt.find_stack(SELF_NAME, ep)
    if st:
        c.save(prod_stack_id=st["Id"])
        return "läuft als Portainer-Stack"
    if not pt.container(ep, SELF_NAME):
        raise ApiError(f"Container „{SELF_NAME}“ auf {srv['name']} nicht gefunden.")
    login, token = _registry(c, pt, srv)
    auth = (login, token) if c.p["private"] else None
    body = Portainer.stack_body(SELF_NAME, c.p["clone_url"], "refs/heads/deploy-prod", _stack_env(e), auth)
    url1, url2 = pt.stack_urls(ep)
    db.set_step(c.pid, "self_switch", "läuft",
                "Umstieg läuft – die Oberfläche ist gleich etwa eine Minute weg und kommt dann von selbst wieder.")
    c.log(f"Umstieg gestartet: Daten ins Volume {e['volume_name']}, danach Neustart als Portainer-Stack")
    pt.run_helper(ep, HELPER_SWITCH, SWITCH_SCRIPT,
                  binds=["/var/run/docker.sock:/var/run/docker.sock", f"{e['volume_name']}:/neu"],
                  env={"KEY": srv["api_key"], "URL1": url1, "URL2": url2, "BODY": json.dumps(body),
                       "ZIEL": "/neu", "TLS": "" if srv["verify_tls"] else "--no-check-certificate"},
                  volumes_from=[SELF_NAME], wait=False)
    time.sleep(300)  # normalerweise wird dieser Container vorher angehalten
    raise ApiError(f"Umstieg hat nicht stattgefunden – Protokoll des Containers {HELPER_SWITCH} in Portainer ansehen.")


def _verify_switch(pid):
    """Nach dem Neustart: hat der Umstieg geklappt? Protokoll übernehmen, Hilfscontainer entfernen."""
    c = Ctx(pid)
    e, srv = c.env("prod")
    pt = c.portainer(srv)
    ep = srv["endpoint_id"]
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            helper = pt.container(ep, HELPER_SWITCH)
            running = helper and helper.get("State", {}).get("Running")
            stack = pt.find_stack(SELF_NAME, ep)
            if not running and (stack or helper):
                logs = pt.logs(ep, HELPER_SWITCH) if helper else ""
                for line in logs.strip().splitlines():
                    c.log(f"Umstieg: {line}")
                if helper:
                    pt.remove(ep, HELPER_SWITCH)
                if stack:
                    c.save(prod_stack_id=stack["Id"])
                    db.set_step(pid, "self_switch", "erledigt",
                                "läuft als Portainer-Stack – Updates kommen ab jetzt über Freigaben. "
                                "Der alte Container „deploy-automatik-alt“ ist angehalten und kann gelöscht werden.")
                else:
                    db.set_step(pid, "self_switch", "fehler",
                                "Umstieg nicht gelungen, die alte Instanz läuft weiter – Details im Protokoll.")
                return
        except ApiError:
            pass
        time.sleep(10)
    db.set_step(pid, "self_switch", "fehler", "Umstieg nicht bestätigt – bitte in Portainer nachsehen.")


def apply_settings(pid, changes, keys_changed):
    """Geänderte Einstellungen übernehmen: Compose-Vorlage (neue Variablen), Stack-Umgebung, Proxy-Ziel, Domains."""
    with _running_lock:
        if pid in _running:
            return False
        _running.add(pid)

    def go():
        c = Ctx(pid)
        try:
            if keys_changed and c.p.get("files_commit") and c.p.get("kind") != "self":
                sha = c.gh.commit_files(c.owner, c.slug, {"deploy/compose.template.yml": compose_template(c.p)},
                                        "Umgebungsvariablen geändert (Compose-Vorlage)")
                c.log(f"Compose-Vorlage mit neuen Variablen ins Repository geschrieben ({sha[:7]}) – "
                      "Testserver bekommt sie mit diesem Build, Produktiv mit der nächsten Freigabe")
            for which, ch in changes.items():
                label = "Test" if which == "test" else "Produktiv"
                e, srv = c.env(which)
                stack_id = c.p.get(f"{which}_stack_id")
                if (ch["port"] or ch["env"]) and stack_id:
                    pt = c.portainer(srv)
                    login, token = c.gh.user()[0], db.get_setting("github_token")
                    pt.redeploy_git_stack(stack_id, srv["endpoint_id"], f"refs/heads/deploy-{which}", _stack_env(e),
                                          (login, token) if c.p["private"] else None)
                    c.log(f"{label}: Stack mit den neuen Einstellungen neu gestartet")
                if ch["port"] and not ch["domain"] and c.p.get(f"{which}_proxy_id"):
                    c.npm().update_forward(c.p[f"{which}_proxy_id"], srv["host"], e["host_port"])
                    c.log(f"{label}: Proxy-Host zeigt jetzt auf {srv['host']}:{e['host_port']}")
                if ch["domain"]:
                    db.set_step(pid, f"{which}_proxy", "offen", "")
        except Exception as ex:  # noqa: BLE001
            c.log(f"✗ Änderung nicht vollständig übernommen: {ex}")
            with _running_lock:
                _running.discard(pid)
            return
        _run(pid)  # offene Proxy-Schritte (neue Domains) abarbeiten; gibt das Projekt am Ende frei

    threading.Thread(target=go, daemon=True).start()
    return True


def resume():
    """Beim Start: Schritte, die beim letzten Beenden mitten im Lauf waren, fortsetzen.
    Läuft vor dem ersten Aufruf der Oberfläche, damit nichts mit neu gestarteten Läufen kollidiert."""
    for p in db.projects():
        for st in db.steps(p["id"]):
            if st["status"] != "läuft":
                continue
            if st["key"] == "self_switch":
                threading.Thread(target=_verify_switch, args=(p["id"],), daemon=True).start()
            else:
                db.set_step(p["id"], st["key"], "offen", "")
                db.add_log(p["id"], "Nach Neustart fortgesetzt")
                start(p["id"])
            break


STEP_FUNCS = {
    "check": step_check,
    "repo": step_repo,
    "files": step_files,
    "actions": step_actions,
    "build": step_build,
    "test_stack": lambda c: _stack(c, "test"),
    "test_proxy": lambda c: _proxy(c, "test"),
    "release": step_release,
    "prod_stack": lambda c: _stack(c, "prod"),
    "prod_proxy": lambda c: _proxy(c, "prod"),
    "self_release": step_self_release,
    "self_wait": step_self_wait,
    "self_switch": step_self_switch,
}
