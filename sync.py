#!/usr/bin/env python3
"""
IServ to Notion Sync — IMAP version
Fetches emails via IMAP and syncs them to a Notion database.
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


def validate_config():
    required = ["ISERV_USERNAME", "ISERV_PASSWORD", "ISERV_URL", "NOTION_TOKEN", "NOTION_DATABASE_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", flush=True)
        sys.exit(1)


def get_imap_host():
    """Extract hostname from ISERV_URL (strip protocol/path)."""
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
    return s.strip()


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
    fetch_ids = list(reversed(fetch_ids))  # newest first

    emails = []
    for mid in fetch_ids:
        try:
            status, fetch_data = imap.fetch(mid, "(UID FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if status != "OK":
                continue

            uid = None
            flags = []
            raw_headers = b""

            for item in fetch_data:
                if isinstance(item, tuple):
                    meta = item[0].decode("utf-8", errors="ignore") if isinstance(item[0], bytes) else str(item[0])
                    content = item[1] if isinstance(item[1], bytes) else b""
                    raw_headers = content

                    uid_match = re.search(r'UID (\d+)', meta)
                    if uid_match:
                        uid = uid_match.group(1)

                    flags_match = re.search(r'FLAGS \(([^)]*)\)', meta)
                    if flags_match:
                        flags = flags_match.group(1).split()

            msg = email.message_from_bytes(raw_headers)
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

            preview = ""
            try:
                status, body_data = imap.fetch(mid, "(BODY.PEEK[1]<0.1000>)")
                if status == "OK":
                    for item in body_data:
                        if isinstance(item, tuple) and isinstance(item[1], bytes):
                            body_text = item[1].decode("utf-8", errors="ignore")
                            preview = strip_html(body_text)[:200]
                            break
            except Exception:
                pass

            if not subject:
                subject = "(Kein Betreff)"
            if not sender:
                sender = "Unbekannt"
            if preview and len(preview) > 200:
                preview = preview[:200] + "..."

            emails.append({
                "uid": uid,
                "subject": subject,
                "sender": sender,
                "date_iso": date_iso,
                "is_read": is_read,
                "preview": preview,
            })

            print(f"  Fetched: {subject[:50]} | from={sender[:30]}", flush=True)

        except Exception as e:
            print(f"  Error fetching message {mid}: {e}", flush=True)

        time.sleep(0.1)

    imap.logout()
    print(f"  Logged out. Fetched {len(emails)} emails.", flush=True)
    return emails


def notion_headers():
    return {
        "Authorization": "Bearer " + NOTION_TOKEN,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_existing_uids():
    existing = set()
    cursor = None
    has_more = True
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
            break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            rt = props.get("IServ UID", {}).get("rich_text", [])
            if rt:
                existing.add(rt[0].get("plain_text", ""))
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return existing


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

    resp = requests.post(
        NOTION_API_URL + "/pages",
        headers=notion_headers(),
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties},
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

    existing = get_existing_uids()
    print(f"  Existing UIDs in Notion: {len(existing)}", flush=True)

    new = skip = err = 0
    for em in emails:
        if em["uid"] and em["uid"] in existing:
            skip += 1
            continue
        try:
            if create_notion_page(em):
                new += 1
            else:
                err += 1
        except Exception as e:
            print(f"  Create failed: {e}", flush=True)
            err += 1
        time.sleep(RATE_LIMIT_SEC)

    print(f"\nSync complete: {new} new | {skip} skipped | {err} errors", flush=True)


if __name__ == "__main__":
    main()
