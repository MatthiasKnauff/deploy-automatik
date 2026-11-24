"""Erkennt, wie auf einem Server die Ordner der Named Volumes angelegt sind (z. B. /docker/<name>),
und schlägt für neue Volumes einen Pfad nach demselben Schema vor."""
from collections import Counter

FALLBACK = "/docker/{name}"


def _template(v):
    opts = v.get("Options") or {}
    dev = (opts.get("device") or "").rstrip("/")
    if not dev.startswith("/") or "bind" not in (opts.get("o") or ""):
        return None
    name = v.get("Name", "")
    labels = v.get("Labels") or {}
    project = labels.get("com.docker.compose.project") or ""
    key = labels.get("com.docker.compose.volume") or ""
    if name and name in dev:
        return dev.replace(name, "{name}")
    if project and key and project in dev and key in dev:
        return dev.replace(project, "{projekt}", 1).replace(key, "{volume}")
    if project and project in dev:
        return dev.replace(project, "{projekt}", 1)
    return dev.rsplit("/", 1)[0] + "/{name}"


def detect(volumes):
    """Gibt (Schema, Anzahl Treffer, Beispiel) zurück. Ohne Treffer: Fallback /docker/{name}."""
    counts, examples = Counter(), {}
    for v in volumes:
        tpl = _template(v)
        if not tpl:
            continue
        counts[tpl] += 1
        examples.setdefault(tpl, f"{v['Name']} → {(v.get('Options') or {}).get('device')}")
    if not counts:
        return FALLBACK, 0, ""
    # bevorzugt Schemata unter /docker, sonst das häufigste
    best = max(counts, key=lambda t: (t.startswith("/docker/"), counts[t]))
    return best, counts[best], examples[best]


def apply(template, name, volume="daten"):
    return template.replace("{name}", name).replace("{projekt}", name).replace("{volume}", volume)
