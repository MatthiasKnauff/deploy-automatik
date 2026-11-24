"""Schnittstellen zu GitHub, Portainer und Nginx Proxy Manager."""
import base64
import os
import time

import requests

TIMEOUT = 30


class ApiError(Exception):
    pass


def _message(r):
    try:
        j = r.json()
        if isinstance(j, dict):
            msg = j.get("message") or j.get("details") or j.get("error") or j
            if isinstance(msg, dict):
                msg = msg.get("message") or str(msg)
            errs = j.get("errors")
            if errs:
                msg = f"{msg} ({errs})"
            return str(msg)
        return str(j)
    except ValueError:
        return (r.text or r.reason or "")[:300]


# ===================================================================== GitHub
class GitHub:
    def __init__(self, token):
        self.api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "deploy-automatik",
        })

    def _req(self, method, path, ok=(200, 201, 204), **kw):
        r = self.s.request(method, self.api + path, timeout=TIMEOUT, **kw)
        if r.status_code not in ok:
            raise ApiError(f"GitHub {method} {path}: {r.status_code} {_message(r)}")
        return r

    def user(self):
        r = self._req("GET", "/user")
        return r.json()["login"], r.headers.get("X-OAuth-Scopes")

    def is_org(self, owner):
        return self._req("GET", f"/users/{owner}").json().get("type") == "Organization"

    def repo(self, owner, name):
        r = self._req("GET", f"/repos/{owner}/{name}", ok=(200, 404))
        return r.json() if r.status_code == 200 else None

    def create_repo(self, owner, name, description, private, org):
        body = {"name": name, "description": description, "private": private,
                "auto_init": True, "has_wiki": False}
        path = f"/orgs/{owner}/repos" if org else "/user/repos"
        return self._req("POST", path, json=body).json()

    def head(self, owner, name, branch="main"):
        for _ in range(10):  # direkt nach dem Anlegen ist der Zweig manchmal noch nicht da
            r = self._req("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}", ok=(200, 404, 409))
            if r.status_code == 200:
                return r.json()["object"]["sha"]
            time.sleep(2)
        raise ApiError(f"Zweig {branch} in {owner}/{name} nicht gefunden")

    def commit_files(self, owner, name, files, message, branch="main"):
        """Alle Dateien in einem einzigen Commit hochladen."""
        parent = self.head(owner, name, branch)
        base_tree = self._req("GET", f"/repos/{owner}/{name}/git/commits/{parent}").json()["tree"]["sha"]
        tree = []
        for p, c in files.items():
            item = {"path": p, "mode": "100755" if p.endswith(".sh") else "100644", "type": "blob"}
            if isinstance(c, bytes):  # Bilder u. ä. als eigener Blob (Base64)
                item["sha"] = self._req("POST", f"/repos/{owner}/{name}/git/blobs",
                                        json={"content": base64.b64encode(c).decode(), "encoding": "base64"}).json()["sha"]
            else:
                item["content"] = c
            tree.append(item)
        tree_sha = self._req("POST", f"/repos/{owner}/{name}/git/trees",
                             json={"base_tree": base_tree, "tree": tree}).json()["sha"]
        commit = self._req("POST", f"/repos/{owner}/{name}/git/commits",
                           json={"message": message, "tree": tree_sha, "parents": [parent]}).json()["sha"]
        self._req("PATCH", f"/repos/{owner}/{name}/git/refs/heads/{branch}", json={"sha": commit})
        return commit

    def file_exists(self, owner, name, path, ref="main"):
        r = self._req("GET", f"/repos/{owner}/{name}/contents/{path}", ok=(200, 404), params={"ref": ref})
        return r.status_code == 200

    def set_workflow_permissions(self, owner, name):
        self._req("PUT", f"/repos/{owner}/{name}/actions/permissions/workflow",
                  json={"default_workflow_permissions": "write", "can_approve_pull_request_reviews": False})

    def runs(self, owner, name, per_page=5):
        return self._req("GET", f"/repos/{owner}/{name}/actions/runs",
                         params={"per_page": per_page}).json().get("workflow_runs", [])

    def branch_exists(self, owner, name, branch):
        return self._req("GET", f"/repos/{owner}/{name}/branches/{branch}", ok=(200, 404)).status_code == 200

    def create_release(self, owner, name, tag, text):
        return self._req("POST", f"/repos/{owner}/{name}/releases",
                         json={"tag_name": tag, "target_commitish": "main", "name": tag, "body": text}).json()

    def latest_release(self, owner, name):
        r = self._req("GET", f"/repos/{owner}/{name}/releases/latest", ok=(200, 404))
        return r.json() if r.status_code == 200 else None


def next_version(tag):
    """v1.4.2 -> v1.4.3; ohne Freigabe -> v0.1.0"""
    if not tag:
        return "v0.1.0"
    parts = tag.lstrip("vV").split(".")
    while len(parts) < 3:
        parts.append("0")
    patch = "".join(ch for ch in parts[2] if ch.isdigit()) or "0"
    return f"v{parts[0]}.{parts[1]}.{int(patch) + 1}"


# ================================================================== Portainer
HELPER_IMAGE = ("docker", "27-cli")
def _docker_logs(raw):
    """Docker-Logstrom (8-Byte-Rahmen) in Text umwandeln."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i < len(raw):
        out.append(raw[i:])
    return b"".join(out).decode("utf-8", "replace")


class Portainer:
    def __init__(self, url, api_key, verify_tls=True):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": api_key})
        self.s.verify = verify_tls

    def _req(self, method, path, ok=(200, 201, 204), **kw):
        try:
            r = self.s.request(method, self.url + path, timeout=kw.pop("timeout", TIMEOUT), **kw)
        except requests.RequestException as e:
            raise ApiError(f"Portainer {self.url} nicht erreichbar: {e}")
        if r.status_code not in ok:
            raise ApiError(f"Portainer {method} {path}: {r.status_code} {_message(r)}")
        return r

    def version(self):
        r = self._req("GET", "/api/system/version", ok=(200, 404))
        if r.status_code == 200:
            return r.json().get("ServerVersion", "?")
        return self._req("GET", "/api/status").json().get("Version", "?")

    def endpoints(self):
        return [{"id": e["Id"], "name": e["Name"]} for e in self._req("GET", "/api/endpoints").json()]

    def used_ports(self, endpoint_id):
        cs = self._req("GET", f"/api/endpoints/{endpoint_id}/docker/containers/json", params={"all": 1}).json()
        used = {}
        for c in cs:
            for p in c.get("Ports", []):
                if p.get("PublicPort"):
                    used[int(p["PublicPort"])] = (c.get("Names") or ["?"])[0].lstrip("/")
        return used

    def find_stack(self, name, endpoint_id):
        for st in self._req("GET", "/api/stacks").json():
            if st.get("Name") == name and int(st.get("EndpointId", -1)) == int(endpoint_id):
                return st
        return None

    def ensure_registry(self, username, token):
        """ghcr.io als Registry hinterlegen, damit auch private Images gezogen werden können."""
        for reg in self._req("GET", "/api/registries").json():
            if reg.get("URL", "").rstrip("/") == "ghcr.io" and reg.get("Username", "") == username:
                return reg["Id"], False
        body = {"Name": f"GitHub ghcr.io ({username})", "Type": 3, "URL": "ghcr.io",
                "Authentication": True, "Username": username, "Password": token}
        return self._req("POST", "/api/registries", json=body).json().get("Id"), True

    # ---- Volumes und Hilfscontainer
    def volumes(self, endpoint_id):
        return self._req("GET", f"/api/endpoints/{endpoint_id}/docker/volumes").json().get("Volumes") or []

    def volume(self, endpoint_id, name):
        r = self._req("GET", f"/api/endpoints/{endpoint_id}/docker/volumes/{name}", ok=(200, 404))
        return r.json() if r.status_code == 200 else None

    def container(self, endpoint_id, name):
        r = self._req("GET", f"/api/endpoints/{endpoint_id}/docker/containers/{name}/json", ok=(200, 404))
        return r.json() if r.status_code == 200 else None

    def run_helper(self, endpoint_id, name, script, binds, env=None, volumes_from=None, wait=True):
        """Kurzlebigen Container (docker:cli) auf dem Server starten, z. B. um einen Ordner anzulegen."""
        base = f"/api/endpoints/{endpoint_id}/docker"
        self._req("POST", f"{base}/images/create", params={"fromImage": HELPER_IMAGE[0], "tag": HELPER_IMAGE[1]},
                  timeout=600)
        self._req("DELETE", f"{base}/containers/{name}", params={"force": "true"}, ok=(204, 404))
        body = {"Image": ":".join(HELPER_IMAGE), "Entrypoint": ["sh", "-c"], "Cmd": [script],
                "Env": [f"{k}={v}" for k, v in (env or {}).items()],
                "Labels": {"deploy-automatik": "hilfscontainer"},
                "HostConfig": {"Binds": binds, "VolumesFrom": volumes_from or []}}
        cid = self._req("POST", f"{base}/containers/create", params={"name": name}, json=body).json()["Id"]
        self._req("POST", f"{base}/containers/{cid}/start", ok=(204, 304))
        if not wait:
            return cid, ""
        res = self._req("POST", f"{base}/containers/{cid}/wait", timeout=300).json()
        logs = _docker_logs(self._req("GET", f"{base}/containers/{cid}/logs",
                                      params={"stdout": 1, "stderr": 1}).content)
        self._req("DELETE", f"{base}/containers/{cid}", params={"force": "true"}, ok=(204, 404))
        if res.get("StatusCode") != 0:
            raise ApiError(f"Hilfscontainer {name} fehlgeschlagen: {logs.strip() or res}")
        return cid, logs

    def logs(self, endpoint_id, name):
        r = self._req("GET", f"/api/endpoints/{endpoint_id}/docker/containers/{name}/logs",
                      params={"stdout": 1, "stderr": 1}, ok=(200, 404))
        return _docker_logs(r.content) if r.status_code == 200 else ""

    def remove(self, endpoint_id, name):
        self._req("DELETE", f"/api/endpoints/{endpoint_id}/docker/containers/{name}",
                  params={"force": "true"}, ok=(204, 404))

    def docker_root(self, endpoint_id):
        """Docker-Datenordner des Servers (bei dir /docker) – Named Volumes liegen darunter in volumes/<name>/_data."""
        return self._req("GET", f"/api/endpoints/{endpoint_id}/docker/info").json().get("DockerRootDir", "")

    @staticmethod
    def stack_body(name, repo_url, ref, env, auth=None, interval="1m"):
        return {
            "name": name,
            "repositoryURL": repo_url,
            "repositoryReferenceName": ref,
            "composeFile": "docker-compose.yml",
            "composeFilePathInRepository": "docker-compose.yml",
            "repositoryAuthentication": bool(auth),
            "repositoryUsername": auth[0] if auth else "",
            "repositoryPassword": auth[1] if auth else "",
            "env": [{"name": k, "value": str(v)} for k, v in env.items()],
            "autoUpdate": {"interval": interval, "forcePullImage": True, "forceUpdate": True},
            "tlsskipVerify": False,
            "fromAppTemplate": False,
        }

    def stack_urls(self, endpoint_id):
        return (f"{self.url}/api/stacks/create/standalone/repository?endpointId={endpoint_id}",
                f"{self.url}/api/stacks?type=2&method=repository&endpointId={endpoint_id}")

    def create_git_stack(self, endpoint_id, name, repo_url, ref, env, auth=None, interval="1m"):
        body = self.stack_body(name, repo_url, ref, env, auth, interval)
        r = self._req("POST", f"/api/stacks/create/standalone/repository?endpointId={endpoint_id}",
                      ok=(200, 201, 404, 405), json=body, timeout=600)
        if r.status_code in (404, 405):  # ältere Portainer-Versionen
            r = self._req("POST", f"/api/stacks?type=2&method=repository&endpointId={endpoint_id}",
                          json=body, timeout=600)
        return r.json()


# ======================================================== Nginx Proxy Manager
class ProxyManager:
    def __init__(self, url, email, password):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        try:
            r = self.s.post(self.url + "/api/tokens", json={"identity": email, "secret": password}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ApiError(f"Proxy Manager {self.url} nicht erreichbar: {e}")
        if r.status_code != 200:
            raise ApiError(f"Proxy Manager Anmeldung fehlgeschlagen: {r.status_code} {_message(r)}")
        self.s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})

    def _req(self, method, path, ok=(200, 201), **kw):
        r = self.s.request(method, self.url + path, timeout=kw.pop("timeout", TIMEOUT), **kw)
        if r.status_code not in ok:
            raise ApiError(f"Proxy Manager {method} {path}: {r.status_code} {_message(r)}")
        return r

    def certificates(self):
        return [{"id": c["id"], "name": c.get("nice_name") or ", ".join(c.get("domain_names", []))}
                for c in self._req("GET", "/api/nginx/certificates").json()]

    def find_host(self, domain):
        for h in self._req("GET", "/api/nginx/proxy-hosts").json():
            if domain.lower() in [d.lower() for d in h.get("domain_names", [])]:
                return h
        return None

    def create_host(self, domain, forward_host, forward_port, certificate, le_email):
        """Normaler Proxy-Host – alles in den Standardfeldern, nichts unter „Erweitert“.

        certificate: "" = ohne SSL, "new" = neues Let's-Encrypt-Zertifikat, sonst ID eines vorhandenen.
        Gibt (host, Hinweis) zurück.
        """
        def body(cert):
            use_ssl = cert not in ("", 0, None)
            b = {
                "domain_names": [domain],
                "forward_scheme": "http",
                "forward_host": forward_host,
                "forward_port": int(forward_port),
                "access_list_id": 0,
                "certificate_id": (cert if cert == "new" else int(cert)) if use_ssl else 0,
                "ssl_forced": use_ssl,
                "http2_support": use_ssl,
                "hsts_enabled": False,
                "hsts_subdomains": False,
                "block_exploits": True,
                "caching_enabled": False,
                "allow_websocket_upgrade": True,
                "advanced_config": "",
                "locations": [],
                "meta": {"letsencrypt_agree": True, "letsencrypt_email": le_email, "dns_challenge": False}
                if cert == "new" else {},
            }
            return b

        try:
            return self._req("POST", "/api/nginx/proxy-hosts", json=body(certificate), timeout=180).json(), ""
        except ApiError as e:
            if certificate != "new":
                raise
            host = self.find_host(domain) or \
                self._req("POST", "/api/nginx/proxy-hosts", json=body(""), timeout=60).json()
            return host, (f"Let's-Encrypt-Zertifikat konnte nicht ausgestellt werden ({e}). "
                          "Proxy-Host ist ohne SSL angelegt – im Proxy Manager unter „SSL“ nachholen, "
                          "sobald die Domain auf den Server zeigt.")
