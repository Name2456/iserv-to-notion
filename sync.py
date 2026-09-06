#!/usr/bin/env python3
"""
IServ to Notion Sync — IMAP version
Fetches emails from the last 30 days via IMAP and syncs them to a Notion database.
Archives all existing entries before syncing to keep content fresh.
Includes full email body as page content and attachments as file properties.
"""

import os
import sys
import time
import re
import imaplib
import email
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
import requests

ISERV_USERNAME = os.environ.get("ISERV_USERNAME")
ISERV_PASSWORD = os.environ.get("ISERV_PASSWORD")
ISERV_URL = os.environ.get("ISERV_URL", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"  # Updated: File Upload API requires newer version
DAYS_BACK = 30
RATE_LIMIT_SEC = 0.35
BODY_MAX_CHARS = 4000
MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5MB max per attachment


def validate_config():
    required = ["ISERV_USERNAME", "ISERV_PASSWORD", "ISERV_URL", "NOTION_TOKEN", "NOTION_DATABASE_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", flush=True)
        sys.exit(1)


def notion_headers():
    return {
        "Authorization": "Bearer " + NOTION_TOKEN,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_imap_host():
    host = ISERV_URL.strip()
    host = host.replace("https://", "").replace("http://", "")
    host = host.replace("/iserv/", "").replace("/iserv", "")
    host = host.rstrip("/")
    return host


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
    s = re.sub(r'<[^>]+>', '', text)
    s = s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    s = s.replace('&quot;', '"').replace('&#39;', "'")
    s = s.replace('&nbsp;', ' ').replace('\xa0', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def ensure_anhang_property():
    """Ensure the 'Anhang' file property exists in the database."""
    try:
        resp = requests.get(
            NOTION_API_URL + "/databases/" + NOTION_DATABASE_ID,
            headers=notion_headers(),
        )
        if resp.status_code == 200:
            props = resp.json().get("properties", {})
            if "Anhang" not in props:
                print("Adding 'Anhang' file property to database...", flush=True)
                update_resp = requests.patch(
                    NOTION_API_URL + "/databases/" + NOTION_DATABASE_ID,
                    headers=notion_headers(),
                    json={"properties": {"Anhang": {"files": {}}}},
                )
                if update_resp.status_code == 200:
                    print("  Added 'Anhang' property", flush=True)
                else:
                    print(f"  Failed to add 'Anhang' property: {update_resp.status_code} {update_resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"  Error ensuring 'Anhang' property: {e}", flush=True)


def delete_all_pages():
    """Archive all existing pages in the Notion database (moves to trash, auto-deleted after 30 days)."""
    print("Deleting existing pages...", flush=True)
    cursor = None
    has_more = True
    count = 0
    while has_more:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            NOTION_API_URL + "/databases/" + NOTION_DATABASE_ID + "/query",
            headers=notion_headers(),
            json=body,
        )
        if resp.status_code != 200:
            print(f"  Query failed: {resp.status_code} {resp.text[:200]}", flush=True)
            break
        data = resp.json()
        for page in data.get("results", []):
            page_id = page["id"]
            arch_resp = requests.patch(
                NOTION_API_URL + "/pages/" + page_id,
                headers=notion_headers(),
                json={"archived": True},
            )
            if arch_resp.status_code == 200:
                count += 1
            time.sleep(0.1)
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    print(f"  Archived {count} existing pages (auto-deleted from trash after 30 days)", flush=True)


def extract_body(msg):
    """Extract text body from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                    return body
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="ignore")
                    return strip_html(html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/html":
                body = strip_html(body)
    return body


def extract_attachments(msg):
    """Extract attachments from email. Returns list of dicts with filename, content, content_type."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                filename = part.get_filename()
                if not filename:
                    continue
                filename = decode_mime(filename)
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if len(payload) > MAX_ATTACHMENT_SIZE:
                    print(f"    Skipping large attachment: {filename} ({len(payload)} bytes)", flush=True)
                    continue
                attachments.append({
                    "filename": filename,
                    "content": payload,
                    "content_type": ct or "application/octet-stream",
                })
    return attachments


def upload_file_to_notion(filename, content, content_type):
    """Upload a file to Notion via the File Upload API. Returns file_upload_id or None."""
    try:
        # Step 1: Create file upload
        create_resp = requests.post(
            NOTION_API_URL + "/file_uploads",
            headers=notion_headers(),
            json={
                "mode": "upload",
                "filename": filename,
                "content_type": content_type,
            },
        )
        if create_resp.status_code != 200:
            print(f"    File upload create failed: {create_resp.status_code} {create_resp.text[:200]}", flush=True)
            return None

        file_upload_id = create_resp.json()["id"]

        # Step 2: Send file contents (multipart/form-data, no JSON content-type)
        send_resp = requests.post(
            NOTION_API_URL + "/file_uploads/" + file_upload_id + "/send",
            headers={
                "Authorization": "Bearer " + NOTION_TOKEN,
                "Notion-Version": NOTION_VERSION,
            },
            files={"file": (filename, content, content_type)},
        )
        if send_resp.status_code != 200:
            print(f"    File upload send failed: {send_resp.status_code} {send_resp.text[:200]}", flush=True)
            return None

        # Step 3: Wait for upload to complete
        for _ in range(10):
            time.sleep(1)
            status_resp = requests.get(
                NOTION_API_URL + "/file_uploads/" + file_upload_id,
                headers=notion_headers(),
            )
            if status_resp.status_code == 200:
                status = status_resp.json().get("status")
                if status == "uploaded":
                    return file_upload_id
                if status == "failed":
                    print(f"    File upload failed for {filename}", flush=True)
                    return None
            time.sleep(1)

        print(f"    File upload timed out for {filename}", flush=True)
        return None
    except Exception as e:
        print(f"    File upload error: {e}", flush=True)
        return None


def fetch_emails_imap():
    host = get_imap_host()
    print(f"Connecting to IMAP {host}:993...", flush=True)

    try:
        imap = imaplib.IMAP4_SSL(host, 993)
    except Exception as e:
        print(f"  Connection to {host} failed: {e}", flush=True)
        alt_host = "imap." + host
        print(f"  Trying {alt_host}:993...", flush=True)
        imap = imaplib.IMAP4_SSL(alt_host, 993)

    print("  Connected", flush=True)

    try:
        imap.login(ISERV_USERNAME, ISERV_PASSWORD)
    except Exception as e:
        print(f"  Login failed with username '{ISERV_USERNAME}': {e}", flush=True)
        email_user = ISERV_USERNAME
        if "@" not in email_user:
            email_user = ISERV_USERNAME + "@" + host
        print(f"  Trying username '{email_user}'...", flush=True)
        imap.login(email_user, ISERV_PASSWORD)

    print("  Login OK", flush=True)

    status, data = imap.select("INBOX")
    if status != "OK":
        print(f"  Select INBOX failed: {status}", flush=True)
        imap.logout()
        return []
    total = int(data[0])
    print(f"  INBOX: {total} messages", flush=True)

    # Search for emails from the last 30 days
    since_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
    print(f"  Searching for emails since {since_date} (last {DAYS_BACK} days)...", flush=True)

    status, messages = imap.search(None, "SINCE " + since_date)
    if status != "OK":
        print(f"  Search failed: {status}", flush=True)
        imap.logout()
        return []

    msg_ids = messages[0].split()
    print(f"  Found {len(msg_ids)} messages in the last {DAYS_BACK} days", flush=True)

    # Sort by newest first (reverse order)
    msg_ids = list(reversed(msg_ids))

    emails = []
    for mid in msg_ids:
        try:
            status, fetch_data = imap.fetch(mid, "(UID FLAGS BODY.PEEK[])")
            if status != "OK":
                continue

            uid = None
            flags = []
            raw_email = b""

            for item in fetch_data:
                if isinstance(item, tuple):
                    meta = item[0].decode("utf-8", errors="ignore") if isinstance(item[0], bytes) else str(item[0])
                    content = item[1] if isinstance(item[1], bytes) else b""
                    raw_email = content

                    uid_match = re.search(r'UID (\d+)', meta)
                    if uid_match:
                        uid = uid_match.group(1)

                    flags_match = re.search(r'FLAGS \(([^)]*)\)', meta)
                    if flags_match:
                        flags = flags_match.group(1).split()

            msg = email.message_from_bytes(raw_email)
            subject = decode_mime(msg.get("Subject", ""))
            sender = decode_mime(msg.get("From", ""))
            date_str = msg.get("Date", "")

            date_iso = None
            if date_str:
                try:
                    dt = parsedate_to_datetime(date_str)
                    date_iso = dt.isoformat()
                except Exception:
                    date_iso = date_str

            is_read = "\\Seen" in flags

            body = extract_body(msg)
            preview = body[:200].strip() if body else ""
            if len(body) > 200:
                preview = preview + "..."

            attachments = extract_attachments(msg)
            if attachments:
                print(f"    Found {len(attachments)} attachment(s)", flush=True)

            if not subject:
                subject = "(Kein Betreff)"
            if not sender:
                sender = "Unbekannt"

            emails.append({
                "uid": uid,
                "subject": subject,
                "sender": sender,
                "date_iso": date_iso,
                "is_read": is_read,
                "preview": preview,
                "body": body[:BODY_MAX_CHARS],
                "attachments": attachments,
            })

            print(f"  Fetched: {subject[:50]} | from={sender[:30]}", flush=True)

        except Exception as e:
            print(f"  Error fetching message {mid}: {e}", flush=True)

        time.sleep(0.1)

    imap.logout()
    print(f"  Logged out. Fetched {len(emails)} emails.", flush=True)
    return emails


def create_notion_page(em):
    properties = {
        "Betreff": {"title": [{"text": {"content": em["subject"][:2000]}}]},
        "Absender": {"rich_text": [{"text": {"content": em["sender"][:2000]}}]},
        "Gelesen": {"checkbox": em["is_read"]},
        "Vorschau": {"rich_text": [{"text": {"content": em["preview"][:2000]}}]},
        "Ordner": {"select": {"name": "Posteingang"}},
    }
    if em["date_iso"]:
        properties["Datum"] = {"date": {"start": em["date_iso"]}}
    if em["uid"]:
        properties["IServ UID"] = {"rich_text": [{"text": {"content": em["uid"]}}]}

    # Upload attachments and add to file property
    file_uploads = []
    for att in em.get("attachments", []):
        file_upload_id = upload_file_to_notion(att["filename"], att["content"], att["content_type"])
        if file_upload_id:
            file_uploads.append({"type": "file_upload", "file_upload": {"id": file_upload_id}, "name": att["filename"]})
            print(f"    Uploaded: {att['filename']}", flush=True)

    if file_uploads:
        properties["Anhang"] = {"files": file_uploads}

    children = []
    if em.get("body"):
        children.append({"object": "block", "type": "divider", "divider": {}})
        remaining = em["body"]
        while remaining and len(children) < 5:
            chunk = remaining[:2000]
            remaining = remaining[2000:]
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                }
            })

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
    if children:
        payload["children"] = children

    resp = requests.post(
        NOTION_API_URL + "/pages",
        headers=notion_headers(),
        json=payload,
    )
    if resp.status_code == 200:
        print(f"  Created: {em['subject'][:60]}", flush=True)
        return True
    else:
        print(f"  Failed ({resp.status_code}): {resp.text[:200]}", flush=True)
        return False


def main():
    print("=" * 60, flush=True)
    print("IServ -> Notion Sync — IMAP version (last 30 days)", flush=True)
    print("Notion API version: " + NOTION_VERSION, flush=True)
    print("=" * 60, flush=True)
    validate_config()

    ensure_anhang_property()
    delete_all_pages()

    try:
        emails = fetch_emails_imap()
    except Exception as e:
        print(f"FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if not emails:
        print("No emails to sync.", flush=True)
        return

    print(f"\nFetched {len(emails)} emails", flush=True)
    for i, em in enumerate(emails[:3]):
        print(f"  Email {i}: subject='{em['subject'][:50]}' sender='{em['sender'][:50]}' uid='{em['uid']}'", flush=True)

    new = err = 0
    for em in emails:
        try:
            if create_notion_page(em):
                new += 1
            else:
                err += 1
        except Exception as e:
            print(f"  Create failed: {e}", flush=True)
            err += 1
        time.sleep(RATE_LIMIT_SEC)

    print(f"\nSync complete: {new} new | {err} errors", flush=True)


if __name__ == "__main__":
    main()