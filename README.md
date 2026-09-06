# IServ → Notion Sync

Syncet täglich die neuesten E-Mails aus dem IServ-Schulportal in eine Notion-Datenbank.
Läuft automatisch als GitHub Actions Cron-Job (täglich 08:00 Berliner Zeit).

## Wie es funktioniert

1. Verbindet sich mit IServ über die [IServAPI](https://pypi.org/project/IServAPI/) Python-Bibliothek
2. Ruft die neuesten 50 E-Mails aus dem Posteingang ab
3. Prüft gegen die Notion-Datenbank, welche E-Mails bereits existieren (Dedup über IServ UID)
4. Erstellt neue Notion-Seiten für jede noch nicht vorhandene E-Mail

## Notion-Datenbank

Die Datenbank **IServ Posteingang** (📧) liegt auf der Seite *Qualifikationsphase 2026–2028*.

| Property | Typ | Beschreibung |
|----------|------|-------------|
| Betreff | Title | E-Mail-Betreff |
| Absender | Rich Text | Absender-Name/E-Mail |
| Datum | Date | Empfangsdatum (mit Uhrzeit) |
| Ordner | Select | Posteingang, Gesendet, Entwürfe, … |
| Gelesen | Checkbox | Gelesen-Status |
| Vorschau | Rich Text | Erste ~200 Zeichen der E-Mail |
| IServ UID | Rich Text | Eindeutige Nachrichten-ID (für Dedup) |

## GitHub Secrets einrichten

Gehe im Repo zu **Settings → Secrets and variables → Actions → New repository secret** und lege diese an:

| Secret | Wert | Hinweis |
|--------|------|--------|
| `ISERV_USERNAME` | IServ-Benutzername | Dein Schul-Account-Name |
| `ISERV_PASSWORD` | IServ-Passwort | Dein Schul-Account-Passwort |
| `ISERV_URL` | `adolfinum.de` | **Nur die Domain!** Kein `https://`, kein `/iserv/` — die Bibliothek hängt `/iserv/` selbst an |
| `NOTION_TOKEN` | Notion-Integration-Token | Siehe unten |
| `NOTION_DATABASE_ID` | Notion-Datenbank-ID | Siehe unten |

### ⚠️ Wichtig: ISERV_URL Format

Die IServAPI-Bibliothek baut URLs so: `{{https://{ISERV_URL}}}/iserv/...`

- ✅ Richtig: `adolfinum.de`
- ❌ Falsch: `https://adolfinum.de/iserv/` (doppeltes `/iserv/`)
- ❌ Falsch: `https://adolfinum.de` (Protocol wird von Bibliothek hinzugefügt)

### Notion-Token & Datenbank-ID finden

1. Gehe zu https://www.notion.so/profile/integrations und erstelle eine neue Internal Integration
2. Kopiere den **Internal Integration Token** (das ist `NOTION_TOKEN`)
3. Öffne die *IServ Posteingang*-Datenbank in Notion → **••• → Connections** → füge die Integration hinzu
4. Die `NOTION_DATABASE_ID` findest du in der URL der Datenbank: `notion.so/DATABASE_ID?v=...` (der Teil vor `?v=`)

## Lokal ausführen

```bash
pip install -r requirements.txt
export ISERV_USERNAME="..."
export ISERV_PASSWORD="..."
export ISERV_URL="adolfinum.de"
export NOTION_TOKEN="secret_..."
export NOTION_DATABASE_ID="..."
python sync.py
```

## Erweiterungsmöglichkeiten

Die IServAPI-Bibliothek unterstützt mehr als nur E-Mails:
- 📅 Kalender/Termine (`get_events`, `get_upcoming_events`)
- 🔔 Benachrichtigungen (`get_notifications`)
- 📁 Dateien (WebDAV)
- 👥 Nutzer-Suche

Das Skript ist so aufgebaut, dass sich diese leicht nachrüsten lassen.

## Lizenz

MIT
