"""Erzeugt den Inhalt eines neuen Repositorys: Startprogramm, Dockerfile, Pipeline, Compose-Vorlage."""

TEMPLATES = {
    "python": "Python (Flask)",
    "node": "Node.js (Express)",
    "static": "Statische Website (nginx)",
}

# ------------------------------------------------------------------ Pipeline
WORKFLOW = r"""name: Build und Bereitstellung

# push auf main      -> Image bauen, Testserver bekommt es automatisch (Zweig deploy-test)
# Release freigeben  -> Image mit Versionsnummer bauen, Produktivserver bekommt es (Zweig deploy-prod)
on:
  push:
    branches: [main]
  release:
    types: [published]
  workflow_dispatch:

permissions:
  contents: write
  packages: write

concurrency:
  group: deploy-${{ github.event_name == 'release' && 'prod' || 'test' }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Ziel und Version bestimmen
        id: meta
        run: bash .github/scripts/meta.sh

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Image bauen und hochladen
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ${{ env.IMAGE }}:${{ steps.meta.outputs.tag }}
            ${{ env.IMAGE }}:${{ steps.meta.outputs.kanal }}
          build-args: |
            APP_VERSION=${{ steps.meta.outputs.version }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Server-Zweig aktualisieren (Portainer holt sich das automatisch)
        run: bash .github/scripts/deploy-branch.sh "${{ steps.meta.outputs.ziel }}" "${{ env.IMAGE }}:${{ steps.meta.outputs.tag }}"

      - name: Zusammenfassung
        run: |
          echo "### ${{ steps.meta.outputs.ziel == 'prod' && 'Produktiv' || 'Test' }}: ${{ steps.meta.outputs.version }}" >> "$GITHUB_STEP_SUMMARY"
          echo "Image: \`${{ env.IMAGE }}:${{ steps.meta.outputs.tag }}\`" >> "$GITHUB_STEP_SUMMARY"
"""

META_SH = r"""#!/usr/bin/env bash
# Bestimmt Ziel (test/prod), Image-Tag und Versionsnummer.
set -euo pipefail
git fetch --tags --force >/dev/null 2>&1 || true

if [ "${GITHUB_EVENT_NAME}" = "release" ]; then
  ZIEL=prod
  TAG="${GITHUB_REF_NAME}"
  VERSION="${GITHUB_REF_NAME}"
  KANAL=latest
else
  ZIEL=test
  LAST=$(git tag -l 'v*' --sort=-v:refname | head -n1)
  if [ -z "$LAST" ]; then
    NEXT=v0.1.0
  else
    IFS=. read -r MA MI PA <<< "${LAST#v}"
    MI=${MI:-0}; PA=${PA:-0}; PA=${PA%%[!0-9]*}
    NEXT="v${MA}.${MI}.$((PA + 1))"
  fi
  TAG="test-${GITHUB_SHA::7}"
  VERSION="$NEXT"
  KANAL=test
fi

{
  echo "ziel=$ZIEL"
  echo "tag=$TAG"
  echo "version=$VERSION"
  echo "kanal=$KANAL"
} >> "$GITHUB_OUTPUT"
echo "IMAGE=ghcr.io/${GITHUB_REPOSITORY,,}" >> "$GITHUB_ENV"
echo "Ziel: $ZIEL  Version: $VERSION  Tag: $TAG"
"""

DEPLOY_BRANCH_SH = r"""#!/usr/bin/env bash
# Schreibt die fertige docker-compose.yml in den Zweig deploy-<ziel>.
# Portainer beobachtet diesen Zweig und aktualisiert den Stack von selbst.
set -euo pipefail
ZIEL="$1"
IMAGE="$2"
BRANCH="deploy-${ZIEL}"
OUT="${RUNNER_TEMP:-/tmp}/docker-compose.yml"

sed -e "s|__IMAGE__|${IMAGE}|g" -e "s|__APP_ENV__|${ZIEL}|g" deploy/compose.template.yml > "$OUT"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git fetch origin "$BRANCH"
  git checkout -f -B "$BRANCH" "origin/$BRANCH"
else
  git checkout -f --orphan "$BRANCH"
  git rm -rf --quiet . || true
  git clean -fdq
fi

cp "$OUT" docker-compose.yml
cat > README.md <<EOF
# ${BRANCH}

Automatisch erzeugt von der Pipeline – nicht von Hand ändern.
Portainer beobachtet diesen Zweig und aktualisiert den Stack, sobald sich hier etwas ändert.
Änderungen an der Compose-Datei bitte in \`main\` unter \`deploy/compose.template.yml\` vornehmen.
EOF
git add docker-compose.yml README.md
if git diff --cached --quiet; then
  echo "Keine Änderung für ${BRANCH}"
else
  git commit -q -m "${ZIEL}: ${IMAGE##*:}"
fi
git push -q origin "$BRANCH"
echo "${BRANCH} -> ${IMAGE}"
"""


def compose_template(p):
    env_keys = sorted(set(p["test"]["env"]) | set(p["prod"]["env"]))
    lines = [
        "# Vorlage für Test- und Produktivserver.",
        "# Image und Umgebung trägt die Pipeline ein; HOST_PORT und die übrigen Variablen kommen aus dem Portainer-Stack.",
        "services:",
        "  app:",
        "    image: __IMAGE__",
        f"    container_name: {p['slug']}-__APP_ENV__",
        "    restart: unless-stopped",
        "    ports:",
        f"      - \"${{HOST_PORT}}:{p['container_port']}\"",
        "    environment:",
        "      APP_ENV: __APP_ENV__",
        "      TZ: Europe/Berlin",
    ]
    lines += [f"      {k}: ${{{k}}}" for k in env_keys]
    if p.get("volume_path"):
        lines += ["    volumes:", f"      - daten:{p['volume_path']}", ""] + VOLUME_BLOCK
    return "\n".join(lines) + "\n"


# Named Volume, das auf dem Server in einem festen Ordner liegt (Schema des Servers, z. B. /docker/<name>).
# Name und Ordner kommen aus dem Portainer-Stack (VOLUME_NAME, DATA_PATH) – je Server und Test/Produktiv getrennt.
VOLUME_BLOCK = [
    "volumes:",
    "  daten:",
    "    name: ${VOLUME_NAME}",
    "    driver: local",
    "    driver_opts:",
    "      type: none",
    "      o: bind",
    "      device: ${DATA_PATH}",
]


# ------------------------------------------------------- die App selbst
SELF_COMPOSE = "\n".join([
    "# Deploy-Automatik selbst – Image trägt die Pipeline ein, Port und Datenordner kommen aus dem Portainer-Stack.",
    "services:",
    "  deploy-automatik:",
    "    image: __IMAGE__",
    "    container_name: deploy-automatik",
    "    restart: unless-stopped",
    "    ports:",
    "      - \"${HOST_PORT}:8000\"",
    "    environment:",
    "      TZ: Europe/Berlin",
    "    volumes:",
    "      - daten:/data",
    "",
] + VOLUME_BLOCK) + "\n"

SELF_FILES = ("app/", "Dockerfile", "requirements.txt", "docker-compose.yml", "ANLEITUNG.md", ".dockerignore")


def generate_self(source_dir):
    """Eigenen Programmcode (aus dem laufenden Container) plus Pipeline für das eigene Repository."""
    import os
    files = {}
    for entry in SELF_FILES:
        full = os.path.join(source_dir, entry)
        if entry.endswith("/"):
            for root, dirs, names in os.walk(full):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for n in names:
                    if n.endswith(".pyc"):
                        continue
                    path = os.path.join(root, n)
                    with open(path, encoding="utf-8") as fh:
                        files[os.path.relpath(path, source_dir)] = fh.read()
        elif os.path.exists(full):
            with open(full, encoding="utf-8") as fh:
                files[entry] = fh.read()
    files.update({
        ".github/workflows/deploy.yml": WORKFLOW,
        ".github/scripts/meta.sh": META_SH,
        ".github/scripts/deploy-branch.sh": DEPLOY_BRANCH_SH,
        "deploy/compose.template.yml": SELF_COMPOSE,
        ".gitignore": "__pycache__/\n*.pyc\n.env\n",
        "README.md": "# Deploy-Automatik\n\nLegt neue Apps mit GitHub-Pipeline, Test- und Produktivserver "
                     "(Portainer) und Proxy-Hosts an. Siehe [ANLEITUNG.md](ANLEITUNG.md).\n\n"
                     "Aktualisieren: Release freigeben – Portainer holt die neue Version von selbst.\n",
    })
    return files


# ------------------------------------------------------------ Startprogramme
PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#f7f7f5;color:#1f2328}}
.banner{{background:#fff4c2;border-bottom:1px solid #e6d58a;padding:8px 16px;font-size:14px}}
main{{max-width:640px;margin:15vh auto;padding:0 16px}}
h1{{font-weight:600}} small{{color:#6b6f76}}
</style></head><body>
{banner}<main><h1>{title}</h1><p>Läuft. Hier entsteht die neue App.</p><small>Version {version}</small></main>
</body></html>"""


def _python(p):
    port = p["container_port"]
    page = PAGE.format(title="{title}", banner="{banner}", version="{version}").replace("{", "{{").replace("}", "}}")
    page = page.replace("{{title}}", "{TITEL}").replace("{{banner}}", "{banner}").replace("{{version}}", "{VERSION}")
    return {
        "app.py": f'''import os

from flask import Flask

app = Flask(__name__)
TITEL = {p["name"]!r}
VERSION = os.environ.get("APP_VERSION", "dev")
APP_ENV = os.environ.get("APP_ENV", "dev")


@app.get("/")
def index():
    banner = f'<div class="banner">Testserver – wird {{VERSION}}</div>' if APP_ENV == "test" else ""
    return f"""{page}"""


@app.get("/health")
def health():
    return {{"status": "ok", "version": VERSION, "umgebung": APP_ENV}}
''',
        "requirements.txt": "flask==3.1.*\ngunicorn==23.*\n",
        "Dockerfile": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION PYTHONUNBUFFERED=1
EXPOSE {port}
CMD ["gunicorn", "-b", "0.0.0.0:{port}", "-w", "2", "app:app"]
""",
        ".dockerignore": ".git\n.github\n__pycache__\n*.pyc\ndeploy\n",
    }


def _node(p):
    port = p["container_port"]
    page = PAGE.format(title="${TITEL}", banner="${banner}", version="${VERSION}")
    return {
        "server.js": f"""const express = require("express");

const app = express();
const TITEL = {p["name"]!r};
const VERSION = process.env.APP_VERSION || "dev";
const APP_ENV = process.env.APP_ENV || "dev";

app.get("/", (req, res) => {{
  const banner = APP_ENV === "test" ? `<div class="banner">Testserver – wird ${{VERSION}}</div>` : "";
  res.send(`{page}`);
}});

app.get("/health", (req, res) => res.json({{ status: "ok", version: VERSION, umgebung: APP_ENV }}));

app.listen({port}, () => console.log(`${{TITEL}} läuft auf Port {port}`));
""",
        "package.json": f"""{{
  "name": "{p['slug']}",
  "version": "0.0.0",
  "private": true,
  "main": "server.js",
  "scripts": {{ "start": "node server.js" }},
  "dependencies": {{ "express": "^4.21.0" }}
}}
""",
        "Dockerfile": f"""FROM node:22-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev
COPY . .
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION NODE_ENV=production
EXPOSE {port}
CMD ["node", "server.js"]
""",
        ".dockerignore": ".git\n.github\nnode_modules\ndeploy\n",
    }


def _static(p):
    port = p["container_port"]
    page = PAGE.format(title=p["name"], banner="<!--BANNER-->", version="__APP_VERSION__")
    return {
        "public/index.html": page + "\n",
        "nginx/default.conf.template": f"""# Wird beim Start vom nginx-Image mit envsubst ausgefüllt (APP_VERSION, APP_ENV).
map "${{APP_ENV}}" $banner {{
    test    '<div class="banner">Testserver – wird ${{APP_VERSION}}</div>';
    default '';
}}

server {{
    listen {port};
    root /usr/share/nginx/html;
    index index.html;

    sub_filter_once off;
    sub_filter '__APP_VERSION__' '${{APP_VERSION}}';
    sub_filter '<!--BANNER-->' $banner;

    location = /health {{
        default_type application/json;
        return 200 '{{"status":"ok","version":"${{APP_VERSION}}","umgebung":"${{APP_ENV}}"}}';
    }}

    location / {{
        try_files $uri $uri/ =404;
    }}
}}
""",
        "Dockerfile": f"""FROM nginx:1.27-alpine
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION APP_ENV=dev
EXPOSE {port}
""",
        ".dockerignore": ".git\n.github\ndeploy\n",
    }


def readme(p, owner):
    repo = f"{owner}/{p['slug']}"
    t, pr = p["test"], p["prod"]
    def ziel(e):
        url = f"https://{e['domain']}" if e.get("domain") else f"http://{e['host']}:{e['host_port']}"
        data = f" · Daten: `{e['data_path']}`" if e.get("data_path") else ""
        return f"{e['server_name']} · Port {e['host_port']} · {url}{data}"
    return f"""# {p['name']}

{p.get('description') or ''}

## So kommt eine Änderung auf die Server

1. **Push auf `main`** – GitHub baut das Image. Die Pipeline schreibt die fertige Compose-Datei in den Zweig
   `deploy-test`, Portainer bemerkt das (spätestens nach einer Minute) und aktualisiert den **Testserver**.
2. **Testserver ansehen** – oben steht „Testserver – wird vX.Y.Z“.
3. **Release freigeben** – [neues Release anlegen](https://github.com/{repo}/releases/new?target=main).
   Die Pipeline baut das Image mit der Versionsnummer, schreibt es in `deploy-prod`, und Portainer
   aktualisiert den **Produktivserver**.

| | Server |
|---|---|
| Test | {ziel(t)} |
| Produktiv | {ziel(pr)} |

## Aufbau

- `deploy/compose.template.yml` – Compose-Datei für beide Server (Ports und Variablen kommen aus Portainer)
- `.github/workflows/deploy.yml` – Pipeline
- Zweige `deploy-test` / `deploy-prod` – werden von der Pipeline geschrieben, nicht von Hand ändern

Eingerichtet mit der Deploy-Automatik.
"""


def generate(p, owner):
    files = {"python": _python, "node": _node, "static": _static}[p["template"]](p)
    files.update({
        ".github/workflows/deploy.yml": WORKFLOW,
        ".github/scripts/meta.sh": META_SH,
        ".github/scripts/deploy-branch.sh": DEPLOY_BRANCH_SH,
        "deploy/compose.template.yml": compose_template(p),
        "README.md": readme(p, owner),
        ".gitignore": "__pycache__/\n*.pyc\nnode_modules/\n.env\n",
    })
    return files
