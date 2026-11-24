# Deploy-Automatik

Legt für eine neue App alles an, was wir bisher von Hand gemacht haben:
GitHub-Repository mit Pipeline → Testserver (jeder Push) → Freigabe per Release → Produktivserver,
jeweils mit Portainer-Stack und Proxy-Host im Nginx Proxy Manager.

## 1. Einmal starten

ZIP auf den Docker-Server kopieren (z. B. Docker 1) und entpacken, dann im Ordner:

    docker compose up -d --build

Danach `http://<server>:8095` öffnen und beim ersten Aufruf ein Passwort festlegen.
Port ändern: in `docker-compose.yml` die Zeile `"8095:8000"`.

## 2. Zugänge eintragen (Einstellungen)

- **Docker-Server**: je Server Portainer-Adresse, API-Schlüssel (Portainer → Benutzer → Access tokens),
  „Umgebungen laden“, Umgebung wählen, IP des Servers für den Proxy. Docker 1 und Docker 2 eintragen.
- **GitHub**: klassisches Token mit `repo`, `workflow`, `write:packages` (Link steht daneben).
- **Nginx Proxy Manager**: Adresse (meist Port 81), Anmelde-E-Mail und Passwort, Zertifikat
  (neues Let's Encrypt je Domain oder vorhandenes Wildcard-Zertifikat).

Alle Zugänge liegen verschlüsselt im Volume `deploy-automatik-daten`.

## 3. Neues Projekt

Name, Vorlage (Python, Node.js, statisch oder eigenes Programm als ZIP), Test- und Produktivserver mit Port und optional Domain.
Die App arbeitet die Schritte ab und zeigt jeden einzeln an:

1. Vorabprüfung – Repo-Name, Ports, Volumes und Domains frei (vorher wird nichts angelegt)
2. Repository mit Startprogramm, Dockerfile und Pipeline
3. erster Build in GitHub
4. Testserver: Portainer-Stack + Proxy-Host
5. wartet auf deine Freigabe (Release in GitHub, Knopf „vX.Y.Z freigeben“ auf der Projektseite)
6. Produktivserver: Portainer-Stack + Proxy-Host

## Daten (Named Volumes)

Jede App bekommt ein Named Volume mit festem Namen, für Test und Produktiv getrennt (`<name>-test`, `<name>-prod`).
Es liegt im Docker-Datenordner des Servers, bei dir also unter `/docker/volumes/<name>/_data`.
Das Formular zeigt beim Anlegen, wo genau. Ist der Docker-Datenordner auf einem Server nicht `/docker`, steht dort ein Hinweis.

## Eigenes Programm hochladen

Vorlage „Eigenes Programm (ZIP hochladen)“: Die ZIP braucht ein `Dockerfile` im obersten Ordner.
Die App übernimmt alle Dateien ins Repository (ohne `node_modules`, `.git`) und legt Pipeline und Compose-Vorlage dazu.
Port im Container und Datenordner im Container trägst du wie bei den Vorlagen ein, Zugangsdaten als Umgebungsvariablen je Server.

**Mehrere Container (z. B. App + Telefon):** Bringt die ZIP eine eigene `deploy/compose.template.yml` mit,
wird sie unverändert übernommen. Platzhalter, die die Pipeline ersetzt:

| Platzhalter | wird zu |
|---|---|
| `__IMAGE__` | Haupt-Image mit Tag (Pflicht) |
| `__IMAGE_BASE__` / `__TAG__` | Image ohne Tag / nur der Tag – für weitere Images: `__IMAGE_BASE__-telefon:__TAG__` |
| `__APP_ENV__` | `test` oder `prod` |

`${HOST_PORT}`, `${VOLUME_NAME}` und die Umgebungsvariablen aus dem Formular setzt Portainer ein.
Weitere Images stehen in `deploy/images.txt`, je Zeile `<name> <ordner>` (z. B. `telefon telefon`);
die Pipeline baut sie aus dem Dockerfile im Ordner mit demselben Tag wie das Haupt-Image – Test und
Produktiv bekommen so immer zusammenpassende Stände. Auch `network_mode: host` ist in der eigenen Vorlage möglich.

## Die App selbst über GitHub aktualisieren

Einstellungen → „Diese App selbst“ → Server wählen, auf dem sie läuft. Die App
1. legt das Repository `deploy-automatik` mit ihrem Programmcode und derselben Pipeline an,
2. trägt die laufende Version als erste Freigabe ein,
3. hält die von Hand gestartete Instanz an, kopiert die Daten (Zugänge, Projekte) ins Volume `deploy-automatik`
   und startet sich als Portainer-Stack neu (etwa eine Minute weg).

Klappt der Stack nicht, startet die alte Instanz automatisch wieder. Danach kommt jede neue Version
über eine Freigabe. Den angehaltenen Container `deploy-automatik-alt` und das alte Volume kannst du löschen.

## So läuft es danach

- **Push auf `main`** → Pipeline baut das Image, schreibt den Zweig `deploy-test` → Portainer
  holt es spätestens nach einer Minute auf den Testserver (Banner „Testserver – wird vX.Y.Z“).
- **Release freigeben** → Pipeline baut `vX.Y.Z`, schreibt `deploy-prod` → Produktivserver.
- Test und Produktiv haben getrennte Stacks, Container und Volumes.
- Proxy-Hosts sind ganz normale Einträge (nichts unter „Erweitert“) und lassen sich im Proxy Manager bearbeiten.
- „Aus Liste entfernen“ löscht nur den Eintrag in der App – Repository, Stacks und Proxy-Hosts bleiben.
