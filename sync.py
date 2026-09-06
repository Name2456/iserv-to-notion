#!/usr/bin/env python3
"""
IServ to Notion Sync — IMAP version
Fetches emails via IMAP and syncs them to a Notion database.
Deletes all existing entries before syncing to keep content fresh.
Includes full email body as page content.
"""

import os
import sys
import time
import re
import imaplib
import email
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
import requests

ISERV_USERNAME = os.environ.get("ISERV_USERNAME")
ISERV_PASSWORD = os.environ.get("ISERV_PASSWORD")
ISERV_URL = os.environ.get("ISERV_URL", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_EMAILS = 50
RATE_LIMIT_SEC = 0.35
BODY_MAX_CHARS = 4000


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


def delete_all_pages():
    """Archive all existing pages in the Notion database."""
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
    print(f"  Deleted {count} existing pages", flush=True)


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

    status, messages = imap.search(None, "ALL")
    if status != "OK":
        print(f"  Search failed: {status}", flush=True)
        imap.logout()
        return []

    msg_ids = messages[0].split()
    print(f"  Found {len(msg_ids)} messages", flush=True)

    fetch_ids = msg_ids[-MAX_EMAILS:] if len(msg_ids) > MAX_EMAILS else msg_ids
    fetch_ids = list(reversed(fetch_ids))

    emails = []
    for mid in fetch_ids:
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
    print("IServ -> Notion Sync — IMAP version", flush=True)
    print("=" * 60, flush=True)
    validate_config()

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
