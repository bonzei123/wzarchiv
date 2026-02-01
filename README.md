# 📰 WZ Archiv - Wormser Zeitung Downloader & DMS

Ein Docker-basiertes Tool zum automatischen Herunterladen, Archivieren und Durchsuchen der Wormser Zeitung (VRM ePaper).

Das Tool bietet ein modernes Web-Interface mit Volltextsuche über alle heruntergeladenen Ausgaben, Benutzerverwaltung und ein digitales Archiv.

## ✨ Features

* **Automatischer Download:** Lädt täglich um **06:00 Uhr** die aktuelle Ausgabe herunter.
* **Auto-Indexierung:** Um **06:15 Uhr** wird der Suchindex automatisch aktualisiert.
* **Archiv-Funktion:** Lückenloses Nachladen von vergangenen Ausgaben über Datums-Suche (Einzeln oder als Zeitraum).
* **Volltextsuche:** Indiziert PDF-Inhalte automatisch (OCR/Text-Extraktion) für schnelle Suche innerhalb der Artikel.
* **Benutzerverwaltung:**
    * **Admin:** Darf Downloads starten, Index neu bauen, Archiv nutzen.
    * **Gast:** Darf nur suchen, lesen und downloaden (Read-Only).
* **Responsive UI:** Kachel-Design mit Vorschau-Snippets, "Gelesen"-Status und direktem PDF-Viewer.
* **Session Management:** Automatischer Logout beim Anbieter, um Session-Limits zu vermeiden.

---

## 🚀 Installation & Start

Du benötigst auf deinem Server/NAS (z.B. Synology) lediglich Docker. Es müssen keine Dateien gebaut werden, das Image wird direkt geladen.

### 1. Ordnerstruktur

Erstelle einen Ordner auf deinem Server. Darin benötigst du **zwei Dateien** und einen Unterordner für die Daten:

```text
/wzarchiv/
├── .env                 # Deine Passwörter (siehe unten)
├── docker-compose.yml   # Docker Konfiguration (siehe unten)
└── downloads/           # Hier landen die PDFs (wird automatisch erstellt)
```

### 2. Konfiguration (.env)
Erstelle eine Datei namens .env und fülle sie mit deinen Daten:

```code
# --- VRM / E-Paper Login ---
# Die URL zur Dashboard-Ansicht
PAPER_URL=epaper.url
PAPER_USER=deine_email@provider.de
PAPER_PASS=dein_epaper_passwort

# --- Webinterface Login (Admin) ---
# Der Admin darf Downloads starten und den Suchindex neu bauen
WEB_USER_ADMIN=admin
WEB_PASS_ADMIN=sicheres_passwort_admin

# --- Webinterface Login (Gast) ---
# Der Gast darf nur suchen und lesen
WEB_USER_GUEST=gast
WEB_PASS_GUEST=gast_passwort

# --- System Einstellungen ---
# Zufällige Zeichenkette für die Session-Verschlüsselung (z.B. "xh7s8d9f...")
SECRET_KEY=bitte_hier_was_zufaelliges_eintragen

# Optional: Discord Webhook für Statusmeldungen (leer lassen zum Deaktivieren)
DISCORD_WEBHOOK_URL=

# Optional: Proxy Server (leer lassen falls nicht benötigt)
PROXY_SERVER=

# Interne Docker Variable (nicht ändern)
RUNNING_IN_DOCKER=true
```

### 3. Docker Compose (docker-compose.yml)
Erstelle eine docker-compose.yml im selben Ordner:

```text
version: '3.8'

services:
  zeitung-downloader:
    image: ghcr.io/bonzei123/wzarchiv:latest
    container_name: zeitung-downloader
    restart: always
    ports:
      - "5000:5000"
    environment:
      - RUNNING_IN_DOCKER=true
      - TZ=Europe/Berlin
    volumes:
      # Hier werden die PDFs gespeichert
      - ./downloads:/app/downloads
    env_file:
      - .env
```

### 4. Starten
Führe im Verzeichnis folgenden Befehl aus:

```code
docker-compose up -d
```

## 🔄 Updates
Um auf die neueste Version zu aktualisieren (ohne Datenverlust), nutze folgenden Befehl. Dieser lädt das neueste Image, startet den Container neu und löscht alte Image-Reste.

```Bash
docker-compose pull && docker-compose up -d && docker image prune -f
```
## ℹ️ Hinweise
Nicht Indexiert: Wenn eine Zeitung frisch heruntergeladen wurde, erscheint sie ggf. mit einem gelben Badge "Nicht Indexiert". Der Textinhalt ist dann noch nicht durchsuchbar. Der Indexer läuft im Hintergrund oder automatisch um 06:15 Uhr.

Browser-Cache: Wenn du dich als Admin ausloggst und als Gast einloggen willst (oder umgekehrt), musst du oft den Browser komplett schließen oder ein Inkognito-Fenster nutzen, da Browser die Login-Daten cachen.
