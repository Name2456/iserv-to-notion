#!/usr/bin/env python3
"""
IServ -> Notion Sync v3

1. E-Mails (IMAP, letzte 30 Tage)          -> Notion "IServ Posteingang"
2. Messenger-Dateien (Matrix API, 30 Tage) -> Notion "IServ Messenger"

Beide Syncs sind INKREMENTELL:
- Neue Eintraege werden angelegt (Dateien nur einmal hochgeladen)
- Eintraege ausserhalb des Zeitfensters werden in den Papierkorb verschoben
- Gelesen-Status von Mails wird aktualisiert
- Mails, die frueher ohne Anhang gespeichert wurden, werden einmalig repariert
- Eintraege mit angekreuzter "Ignorieren"-Checkbox bleiben unangetastet stehen
  und werden nicht neu angelegt, auch wenn die Quelle sie noch liefert.

Der IServ-Messenger basiert auf Matrix; der Homeserver ist die IServ-Domain.
Login erfolgt mit den normalen IServ-Zugangsdaten (m.login.password).
"""

import os
import sys
import time
import re
import json
import imaplib
import email
import traceback
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
ISERV_USERNAME = os.environ.get("ISERV_USERNAME", "")
ISERV_PASSWORD = os.environ.get("ISERV_PASSWORD", "")
ISERV_URL = os.environ.get("ISERV_URL", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
# Messenger-Datenbank: kann per Secret ueberschrieben werden, Default = die angelegte DB
NOTION_MESSENGER_DATABASE_ID = os.environ.get("NOTION_MESSENGER_DATABASE_ID") or "0700ab1a5abc46a28b9377d0b018d636"
MESSENGER_ENABLED = os.environ.get("MESSENGER_ENABLED", "true").lower() not in ("0", "false", "no")

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
DAYS_BACK = 30
RATE_LIMIT_SEC = 0.35
BODY_MAX_CHARS = 4000
MAX_FILE_SIZE = 20 * 1024 * 1024  # Notion single_part Limit
# Notion zaehlt Zeichen in UTF-16 (Emojis = 2) -> Puffer unter dem 2000er Limit lassen
TEXT_LIMIT = 1900
HTTP_TIMEOUT = 60

MATRIX_FILE_MSGTYPES = {
    "m.file": "Dokument",
    "m.image": "Bild",
    "m.audio": "Audio",
    "m.video": "Video",
}


def log(msg):
    print(msg, flush=True)


def validate_config():
    required = ["ISERV_USERNAME", "ISERV_PASSWORD", "ISERV_URL", "NOTION_TOKEN", "NOTION_DATABASE_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log(f"Missing env vars: {', '.join(missing)}")
        sys.exit(1)


def iserv_host():
    host = ISERV_URL.strip().replace("https://", "").replace("http://", "")
    return host.split("/")[0].strip()


# ---------------------------------------------------------------------------
# Notion Helfer
# ---------------------------------------------------------------------------
def notion_headers(json_body=True):
    h = {"Authorization": "Bearer " + NOTION_TOKEN, "Notion-Version": NOTION_VERSION}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def notion_request(method, path, **kwargs):
    """Request mit Retry bei 429/5xx."""
    url = NOTION_API_URL + path
    for attempt in range(4):
        resp = requests.request(method, url, headers=notion_headers(), timeout=HTTP_TIMEOUT, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", 2)) if resp.status_code == 429 else 2 * (attempt + 1)
            log(f"    Notion {resp.status_code} -> warte {wait:.1f}s")
            time.sleep(wait)
            continue
        return resp
    return resp


def get_data_source_id(database_id):
    resp = notion_request("GET", "/databases/" + database_id)
    if resp.status_code != 200:
        raise Exception(f"Failed to get database {database_id}: {resp.status_code} {resp.text[:200]}")
    data_sources = resp.json().get("data_sources", [])
    if not data_sources:
        raise Exception("No data sources found for database " + database_id)
    return data_sources[0]["id"]


def ensure_property(data_source_id, name, schema):
    try:
        resp = notion_request("GET", "/data_sources/" + data_source_id)
        if resp.status_code == 200 and name not in resp.json().get("properties", {}):
            log(f"  Adding property '{name}'...")
            upd = notion_request("PATCH", "/data_sources/" + data_source_id, json={"properties": {name: schema}})
            if upd.status_code != 200:
                log(f"  Failed: {upd.status_code} {upd.text[:200]}")
    except Exception as e:
        log(f"  Error ensuring property '{name}': {e}")


def query_all_pages(data_source_id):
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion_request("POST", "/data_sources/" + data_source_id + "/query", json=body)
        if resp.status_code != 200:
            raise Exception(f"Query failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def prop_plain_text(page, name):
    prop = page.get("properties", {}).get(name, {})
    parts = prop.get("rich_text") or prop.get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def prop_checkbox(page, name):
    return bool(page.get("properties", {}).get(name, {}).get("checkbox", False))


def prop_has_files(page, name):
    return len(page.get("properties", {}).get(name, {}).get("files", []) or []) > 0


def trash_page(page_id):
    resp = notion_request("PATCH", "/pages/" + page_id, json={"in_trash": True})
    time.sleep(0.1)
    return resp.status_code == 200


def upload_file_to_notion(filename, content, content_type):
    """Datei per File Upload API hochladen. Gibt file_upload_id oder None zurueck."""
    try:
        if len(content) > MAX_FILE_SIZE:
            log(f"    Skipping large file: {filename} ({len(content)} bytes)")
            return None
        create_resp = notion_request("POST", "/file_uploads", json={"mode": "single_part", "filename": filename[:900], "content_type": content_type})
        if create_resp.status_code != 200:
            log(f"    File upload create failed: {create_resp.status_code} {create_resp.text[:200]}")
            return None
        file_upload_id = create_resp.json()["id"]
        send_resp = requests.post(
            NOTION_API_URL + "/file_uploads/" + file_upload_id + "/send",
            headers=notion_headers(json_body=False),
            files={"file": (filename, content, content_type)},
            timeout=HTTP_TIMEOUT * 2,
        )
        if send_resp.status_code != 200:
            log(f"    File upload send failed: {send_resp.status_code} {send_resp.text[:200]}")
            return None
        if send_resp.json().get("status") == "uploaded":
            return file_upload_id
        for _ in range(10):
            time.sleep(1)
            st = notion_request("GET", "/file_uploads/" + file_upload_id)
            if st.status_code == 200:
                status = st.json().get("status")
                if status == "uploaded":
                    return file_upload_id
                if status == "failed":
                    log(f"    File upload failed for {filename}")
                    return None
        log(f"    File upload timed out for {filename}")
        return None
    except Exception as e:
        log(f"    File upload error: {e}")
        return None


def select_name(value):
    """Select-Optionen duerfen kein Komma enthalten und max. 100 Zeichen lang sein."""
    value = (value or "").replace(",", " /").strip()
    return value[:100] or "Unbekannt"


def rt(value, limit=TEXT_LIMIT):
    return {"rich_text": [{"text": {"content": (value or "")[:limit]}}]}


# ---------------------------------------------------------------------------
# E-Mail (IMAP)
# ---------------------------------------------------------------------------
def decode_mime(value):
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def strip_html(text):
    if not text:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def extract_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return strip_html(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    if msg.get_content_type() == "text/html":
        body = strip_html(body)
    return body


def extract_attachments(msg):
    """Alle Teile mit Dateinamen (attachment ODER inline, z.B. eingebettete Bilder/PDFs)."""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if not filename and "attachment" not in disposition:
            continue
        filename = decode_mime(filename) if filename else "anhang.bin"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > MAX_FILE_SIZE:
            log(f"    Skipping large attachment: {filename} ({len(payload)} bytes)")
            continue
        attachments.append({
            "filename": filename,
            "content": payload,
            "content_type": part.get_content_type() or "application/octet-stream",
        })
    return attachments


def parse_fetch_meta(fetch_data):
    """Liefert (flags, raw_bytes) aus einer imaplib-FETCH-Antwort."""
    flags = []
    raw = b""
    for item in fetch_data:
        if isinstance(item, tuple):
            meta = item[0].decode("utf-8", errors="ignore") if isinstance(item[0], bytes) else str(item[0])
            if isinstance(item[1], bytes) and len(item[1]) > len(raw):
                raw = item[1]
            m = re.search(r"FLAGS \(([^)]*)\)", meta)
            if m:
                flags = m.group(1).split()
        elif isinstance(item, bytes):
            m = re.search(r"FLAGS \(([^)]*)\)", item.decode("utf-8", errors="ignore"))
            if m and not flags:
                flags = m.group(1).split()
    return flags, raw


def imap_connect():
    host = iserv_host()
    log(f"Connecting to IMAP {host}:993...")
    try:
        imap = imaplib.IMAP4_SSL(host, 993)
    except Exception as e:
        log(f"  Connection to {host} failed: {e}")
        alt_host = "imap." + host
        log(f"  Trying {alt_host}:993...")
        imap = imaplib.IMAP4_SSL(alt_host, 993)
    try:
        imap.login(ISERV_USERNAME, ISERV_PASSWORD)
    except Exception as e:
        log(f"  Login failed with username '{ISERV_USERNAME}': {e}")
        email_user = ISERV_USERNAME + ("@" + host if "@" not in ISERV_USERNAME else "")
        log(f"  Trying username '{email_user}'...")
        imap.login(email_user, ISERV_PASSWORD)
    log("  IMAP login OK")
    return imap


def fetch_emails_imap(full_fetch_needed):
    """
    full_fetch_needed(uid) -> True, wenn die Mail komplett geladen werden muss
    (neu oder Anhang-Reparatur). Sonst werden nur die FLAGS geholt.
    Rueckgabe: Liste von dicts mit uid, is_read, full (bool) und ggf. Inhalt.
    """
    imap = imap_connect()
    try:
        status, data = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise Exception(f"Select INBOX failed: {status}")
        since_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
        status, result = imap.uid("SEARCH", None, "SINCE", since_date)
        if status != "OK":
            raise Exception(f"UID SEARCH failed: {status}")
        uids = [u.decode() for u in result[0].split()]
        uids.reverse()
        log(f"  {len(uids)} Mails in den letzten {DAYS_BACK} Tagen")
        emails = []
        full_count = 0
        for uid in uids:
            try:
                if full_fetch_needed(uid):
                    status, fetch_data = imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
                    if status != "OK":
                        continue
                    flags, raw = parse_fetch_meta(fetch_data)
                    msg = email.message_from_bytes(raw)
                    subject = decode_mime(msg.get("Subject", "")) or "(Kein Betreff)"
                    sender = decode_mime(msg.get("From", "")) or "Unbekannt"
                    date_iso = None
                    if msg.get("Date"):
                        try:
                            date_iso = parsedate_to_datetime(msg.get("Date")).isoformat()
                        except Exception:
                            date_iso = None
                    body = extract_body(msg)
                    preview = (body[:200].strip() + ("..." if len(body) > 200 else "")) if body else ""
                    attachments = extract_attachments(msg)
                    emails.append({
                        "uid": uid, "full": True, "is_read": "\\Seen" in flags,
                        "subject": subject, "sender": sender, "date_iso": date_iso,
                        "preview": preview, "body": body[:BODY_MAX_CHARS], "attachments": attachments,
                    })
                    full_count += 1
                else:
                    status, fetch_data = imap.uid("FETCH", uid, "(FLAGS)")
                    if status != "OK":
                        continue
                    flags, _ = parse_fetch_meta(fetch_data)
                    emails.append({"uid": uid, "full": False, "is_read": "\\Seen" in flags})
            except Exception as e:
                log(f"  Error fetching UID {uid}: {e}")
        log(f"  {full_count} Mails komplett geladen, {len(emails) - full_count} nur Status geprueft")
        return emails
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def create_email_page(em, data_source_id):
    properties = {
        "Betreff": {"title": [{"text": {"content": em["subject"][:TEXT_LIMIT]}}]},
        "Absender": rt(em["sender"]),
        "Gelesen": {"checkbox": em["is_read"]},
        "Vorschau": rt(em["preview"]),
        "Ordner": {"select": {"name": "Posteingang"}},
        "IServ UID": rt(em["uid"]),
    }
    if em.get("date_iso"):
        properties["Datum"] = {"date": {"start": em["date_iso"]}}
    file_uploads = []
    for att in em.get("attachments", []):
        fid = upload_file_to_notion(att["filename"], att["content"], att["content_type"])
        if fid:
            file_uploads.append({"type": "file_upload", "file_upload": {"id": fid}, "name": att["filename"][:100]})
            log(f"    Uploaded: {att['filename']}")
    if file_uploads:
        properties["Anhang"] = {"files": file_uploads}
    children = []
    if em.get("body"):
        children.append({"object": "block", "type": "divider", "divider": {}})
        remaining = em["body"]
        while remaining and len(children) < 5:
            chunk, remaining = remaining[:TEXT_LIMIT], remaining[TEXT_LIMIT:]
            children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})
    payload = {"parent": {"data_source_id": data_source_id}, "properties": properties}
    if children:
        payload["children"] = children
    resp = notion_request("POST", "/pages", json=payload)
    if resp.status_code == 200:
        log(f"  + {em['subject'][:60]}" + (f"  [{len(file_uploads)} Anhang/Anhaenge]" if file_uploads else ""))
        return True
    log(f"  Failed ({resp.status_code}): {resp.text[:200]}")
    return False


def sync_emails():
    log("-" * 60)
    log("E-MAILS")
    log("-" * 60)
    ds_id = get_data_source_id(NOTION_DATABASE_ID)
    ensure_property(ds_id, "Anhang", {"files": {}})
    ensure_property(ds_id, "Ignorieren", {"checkbox": {}})

    existing = {}      # uid -> {page_id, is_read, has_attachment, ignored}
    to_trash = []      # Seiten ohne UID / Duplikate
    for page in query_all_pages(ds_id):
        uid = prop_plain_text(page, "IServ UID")
        if not uid or uid in existing:
            to_trash.append(page["id"])
            continue
        existing[uid] = {"page_id": page["id"], "is_read": prop_checkbox(page, "Gelesen"), "has_attachment": prop_has_files(page, "Anhang"), "ignored": prop_checkbox(page, "Ignorieren")}
    log(f"  {len(existing)} Mails bereits in Notion")

    def needs_full(uid):
        # Neu -> komplett laden. Bekannt ohne Anhang -> komplett laden, um evtl. fehlende Anhaenge nachzuziehen.
        return uid not in existing or not existing[uid]["has_attachment"]

    emails = fetch_emails_imap(needs_full)
    present = set()
    created = updated = repaired = errors = 0
    for em in emails:
        uid = em["uid"]
        present.add(uid)
        try:
            if uid in existing:
                ex = existing[uid]
                if ex.get("ignored"):
                    continue
                if em["full"] and em.get("attachments") and not ex["has_attachment"]:
                    # Frueher ohne Anhang gespeichert -> neu anlegen
                    trash_page(ex["page_id"])
                    if create_email_page(em, ds_id):
                        repaired += 1
                    else:
                        errors += 1
                elif em["is_read"] != ex["is_read"]:
                    resp = notion_request("PATCH", "/pages/" + ex["page_id"], json={"properties": {"Gelesen": {"checkbox": em["is_read"]}}})
                    if resp.status_code == 200:
                        updated += 1
                    time.sleep(0.1)
            else:
                if create_email_page(em, ds_id):
                    created += 1
                else:
                    errors += 1
                time.sleep(RATE_LIMIT_SEC)
        except Exception as e:
            log(f"  Error for UID {uid}: {e}")
            errors += 1

    for uid, ex in existing.items():
        if uid not in present and not ex.get("ignored"):
            to_trash.append(ex["page_id"])
    trashed = sum(1 for pid in to_trash if trash_page(pid))
    log(f"E-Mails: {created} neu | {repaired} repariert | {updated} Status aktualisiert | {trashed} entfernt | {errors} Fehler")
    return errors == 0


# ---------------------------------------------------------------------------
# Messenger (Matrix)
# ---------------------------------------------------------------------------
def matrix_base_url():
    host = iserv_host()
    try:
        r = requests.get("https://" + host + "/.well-known/matrix/client", timeout=15)
        if r.status_code == 200:
            base = r.json().get("m.homeserver", {}).get("base_url")
            if base:
                return base.rstrip("/")
    except Exception:
        pass
    return "https://" + host


class Matrix:
    def __init__(self, base):
        self.base = base
        self.token = None
        self.user_id = None

    def _req(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(5):
            resp = requests.request(method, self.base + path, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code == 429:
                try:
                    wait = resp.json().get("retry_after_ms", 2000) / 1000.0
                except Exception:
                    wait = 2.0
                log(f"    Matrix 429 -> warte {wait:.1f}s")
                time.sleep(min(wait, 30))
                continue
            return resp
        return resp

    def login(self, username, password):
        candidates = [username]
        if "@" in username and ":" not in username:
            candidates.append(username.split("@")[0])
        candidates.append(f"@{username.split('@')[0]}:{iserv_host()}")
        last = None
        for user in candidates:
            body = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": user},
                "user": user,  # Legacy-Feld als Fallback fuer aeltere Server
                "password": password,
                "initial_device_display_name": "Notion Sync (GitHub Actions)",
            }
            resp = self._req("POST", "/_matrix/client/v3/login", json=body)
            if resp.status_code == 200:
                data = resp.json()
                self.token = data["access_token"]
                self.user_id = data.get("user_id")
                log(f"  Matrix login OK als {self.user_id}")
                return True
            last = f"{resp.status_code} {resp.text[:200]}"
            log(f"  Matrix login als '{user}' fehlgeschlagen: {last}")
        raise Exception("Matrix login failed: " + str(last))

    def logout(self):
        if not self.token:
            return
        try:
            self._req("POST", "/_matrix/client/v3/logout", json={})
        except Exception:
            pass
        self.token = None

    def joined_rooms(self):
        resp = self._req("GET", "/_matrix/client/v3/joined_rooms")
        if resp.status_code != 200:
            raise Exception(f"joined_rooms failed: {resp.status_code} {resp.text[:200]}")
        return resp.json().get("joined_rooms", [])

    def room_members(self, room_id):
        resp = self._req("GET", f"/_matrix/client/v3/rooms/{room_id}/joined_members")
        if resp.status_code != 200:
            return {}
        return {uid: (info or {}).get("display_name") or uid for uid, info in resp.json().get("joined", {}).items()}

    def room_name(self, room_id, members):
        resp = self._req("GET", f"/_matrix/client/v3/rooms/{room_id}/state/m.room.name/")
        if resp.status_code == 200:
            name = resp.json().get("name")
            if name:
                return name
        others = [n for uid, n in members.items() if uid != self.user_id]
        if others:
            return ", ".join(sorted(others)[:3])
        return room_id

    def iter_messages(self, room_id, cutoff_ms):
        """Nachrichten rueckwaerts bis zum Cutoff-Zeitpunkt."""
        params = {"dir": "b", "limit": 100}
        seen_encrypted = 0
        for _ in range(50):  # Sicherheitsgrenze: 5000 Events pro Raum
            resp = self._req("GET", f"/_matrix/client/v3/rooms/{room_id}/messages", params=params)
            if resp.status_code != 200:
                log(f"    messages failed for {room_id}: {resp.status_code} {resp.text[:150]}")
                return
            data = resp.json()
            chunk = data.get("chunk", [])
            if not chunk:
                return
            reached_cutoff = False
            for ev in chunk:
                ts = ev.get("origin_server_ts", 0)
                if ts < cutoff_ms:
                    reached_cutoff = True
                    continue
                if ev.get("type") == "m.room.encrypted":
                    seen_encrypted += 1
                    continue
                if ev.get("type") == "m.room.message":
                    yield ev
            if reached_cutoff or not data.get("end"):
                break
            params["from"] = data["end"]
        if seen_encrypted:
            log(f"    Hinweis: {seen_encrypted} verschluesselte Nachrichten uebersprungen (E2EE nicht unterstuetzt)")

    def download(self, mxc_url):
        m = re.match(r"mxc://([^/]+)/(.+)", mxc_url or "")
        if not m:
            return None, None
        server, media_id = m.group(1), m.group(2)
        for path in (f"/_matrix/client/v1/media/download/{server}/{media_id}", f"/_matrix/media/v3/download/{server}/{media_id}"):
            resp = self._req("GET", path, params={"allow_redirect": "true"}, allow_redirects=True)
            if resp.status_code == 200:
                return resp.content, resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        log(f"    Download failed for {mxc_url}: {resp.status_code}")
        return None, None


def collect_messenger_files(mx, cutoff_ms):
    files = []
    rooms = mx.joined_rooms()
    log(f"  {len(rooms)} Chats/Raeume")
    for room_id in rooms:
        members = mx.room_members(room_id)
        name = mx.room_name(room_id, members)
        count = 0
        for ev in mx.iter_messages(room_id, cutoff_ms):
            content = ev.get("content", {}) or {}
            msgtype = content.get("msgtype")
            if msgtype not in MATRIX_FILE_MSGTYPES:
                continue
            url = content.get("url")
            if not url:
                if content.get("file"):
                    log("    Verschluesselte Datei uebersprungen")
                continue
            filename = content.get("filename") or content.get("body") or "datei"
            caption = content.get("body", "")
            if caption == filename:
                caption = ""
            info = content.get("info", {}) or {}
            files.append({
                "event_id": ev.get("event_id"),
                "ts": ev.get("origin_server_ts", 0),
                "sender": members.get(ev.get("sender"), ev.get("sender", "")),
                "chat": name,
                "typ": MATRIX_FILE_MSGTYPES[msgtype],
                "filename": filename,
                "caption": caption,
                "url": url,
                "mimetype": info.get("mimetype") or "application/octet-stream",
                "size": info.get("size") or 0,
            })
            count += 1
        if count:
            log(f"    {name[:40]}: {count} Datei(en)")
    return files


def create_messenger_page(mx, f, data_source_id):
    content, ctype = mx.download(f["url"])
    if content is None:
        return False
    fid = upload_file_to_notion(f["filename"], content, f["mimetype"] if f["mimetype"] != "application/octet-stream" else ctype)
    properties = {
        "Name": {"title": [{"text": {"content": f["filename"][:TEXT_LIMIT]}}]},
        "Chat": {"select": {"name": select_name(f["chat"])}},
        "Absender": rt(f["sender"]),
        "Datum": {"date": {"start": datetime.fromtimestamp(f["ts"] / 1000.0, tz=timezone.utc).isoformat()}},
        "Typ": {"select": {"name": f["typ"]}},
        "Nachricht": rt(f["caption"]),
        "Event ID": rt(f["event_id"]),
    }
    if fid:
        properties["Datei"] = {"files": [{"type": "file_upload", "file_upload": {"id": fid}, "name": f["filename"][:100]}]}
    resp = notion_request("POST", "/pages", json={"parent": {"data_source_id": data_source_id}, "properties": properties})
    if resp.status_code == 200:
        log(f"  + {f['filename'][:50]}  ({f['chat'][:30]})" + ("" if fid else "  [Upload fehlgeschlagen]"))
        return True
    log(f"  Failed ({resp.status_code}): {resp.text[:200]}")
    return False


def sync_messenger():
    log("-" * 60)
    log("MESSENGER")
    log("-" * 60)
    ds_id = get_data_source_id(NOTION_MESSENGER_DATABASE_ID)
    ensure_property(ds_id, "Ignorieren", {"checkbox": {}})

    existing = {}
    to_trash = []
    for page in query_all_pages(ds_id):
        eid = prop_plain_text(page, "Event ID")
        if not eid or eid in existing:
            to_trash.append(page["id"])
            continue
        existing[eid] = {"page_id": page["id"], "ignored": prop_checkbox(page, "Ignorieren")}
    log(f"  {len(existing)} Dateien bereits in Notion")

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).timestamp() * 1000)
    mx = Matrix(matrix_base_url())
    log(f"  Homeserver: {mx.base}")
    created = errors = 0
    present = set()
    try:
        mx.login(ISERV_USERNAME, ISERV_PASSWORD)
        files = collect_messenger_files(mx, cutoff_ms)
        files.sort(key=lambda x: x["ts"])
        log(f"  {len(files)} Dateien in den letzten {DAYS_BACK} Tagen gefunden")
        for f in files:
            present.add(f["event_id"])
            if f["event_id"] in existing:
                continue
            try:
                if create_messenger_page(mx, f, ds_id):
                    created += 1
                else:
                    errors += 1
            except Exception as e:
                log(f"  Error for {f['filename']}: {e}")
                errors += 1
            time.sleep(RATE_LIMIT_SEC)
    finally:
        mx.logout()

    for eid, info in existing.items():
        if eid not in present and not info.get("ignored"):
            to_trash.append(info["page_id"])
    trashed = sum(1 for pid in to_trash if trash_page(pid))
    log(f"Messenger: {created} neu | {trashed} entfernt | {errors} Fehler")
    return errors == 0


# ---------------------------------------------------------------------------
def main():
    log("=" * 60)
    log(f"IServ -> Notion Sync v3 (letzte {DAYS_BACK} Tage, inkrementell)")
    log("Notion API version: " + NOTION_VERSION)
    log("=" * 60)
    validate_config()
    ok = True
    try:
        ok = sync_emails() and ok
    except Exception as e:
        log(f"FEHLER E-Mail-Sync: {e}")
        traceback.print_exc()
        ok = False
    if MESSENGER_ENABLED and NOTION_MESSENGER_DATABASE_ID:
        try:
            ok = sync_messenger() and ok
        except Exception as e:
            log(f"FEHLER Messenger-Sync: {e}")
            if "404" in str(e):
                log("  -> Die Messenger-Datenbank ist nicht mit der Notion-Integration verbunden.")
                log("     In Notion: Datenbank 'IServ Messenger' oeffnen -> ... -> Verbindungen -> Integration hinzufuegen.")
            else:
                traceback.print_exc()
            ok = False
    else:
        log("Messenger-Sync deaktiviert.")
    log("=" * 60)
    log("Fertig." if ok else "Fertig mit Fehlern.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
