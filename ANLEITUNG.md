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

Name, Vorlage (Python, Node.js oder statisch), Test- und Produktivserver mit Port und optional Domain.
Die App arbeitet die Schritte ab und zeigt jeden einzeln an:

1. Vorabprüfung – Repo-Name frei, Ports frei, Domains frei (vorher wird nichts angelegt)
2. Repository mit Startprogramm, Dockerfile und Pipeline
3. erster Build in GitHub
4. Testserver: Portainer-Stack + Proxy-Host
5. wartet auf deine Freigabe (Release in GitHub, Knopf „vX.Y.Z freigeben“ auf der Projektseite)
6. Produktivserver: Portainer-Stack + Proxy-Host

## Datenordner (Named Volumes unter /docker)

Jede App bekommt ein Named Volume, das auf dem Server in einem festen Ordner liegt. Die App liest über
Portainer die vorhandenen Volumes des Zielservers, erkennt das Schema (z. B. `/docker/<projekt>/<volume>`)
und schlägt den Ordner danach vor – mit `-test` bzw. `-prod` im Namen. Der Vorschlag steht im Formular
und lässt sich ändern. Den Ordner legt die App beim Einrichten selbst an.

## Die App selbst über GitHub aktualisieren

Einstellungen → „Diese App selbst“ → Server wählen, auf dem sie läuft. Die App
1. legt das Repository `deploy-automatik` mit ihrem Programmcode und derselben Pipeline an,
2. trägt die laufende Version als erste Freigabe ein,
3. hält die von Hand gestartete Instanz an, kopiert die Daten (Zugänge, Projekte) in den Datenordner
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
