#!/usr/bin/env python3
"""
IServ to Notion Sync — DEBUG BUILD v4
"""

import os
import sys
import json
import time
import logging
import importlib.util
from datetime import datetime
from typing import Dict, List, Any, Optional

import requests

# IServAPI bug workaround
_spec = importlib.util.find_spec("IServAPI")
if _spec and _spec.origin:
    with open(_spec.origin, "r") as _f:
        _content = _f.read()
    if "from turtle import st" in _content:
        _content = _content.replace("from turtle import st", "from typing import Any, Literal")
        _content = _content.replace("\nclass IServAPI:", "\nAlarmType = Any\nRecurring = dict\n\nclass IServAPI:")
        with open(_spec.origin, "w") as _f:
            _f.write(_content)

from IServAPI import IServAPI

ISERV_USERNAME = os.environ.get("ISERV_USERNAME")
ISERV_PASSWORD = os.environ.get("ISERV_PASSWORD")
ISERV_URL = os.environ.get("ISERV_URL")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_EMAILS = 50
RATE_LIMIT_SEC = 0.35

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def validate_config() -> None:
    required = ["ISERV_USERNAME", "ISERV_PASSWORD", "ISERV_URL", "NOTION_TOKEN", "NOTION_DATABASE_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing: {', '.join(missing)}")
        sys.exit(1)


def _find_lists(obj: Any, path: str = "") -> List[tuple]:
    """Recursively find all lists in a nested dict/list structure."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_find_lists(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        if obj:
            results.append((path, len(obj), type(obj[0]).__name__))
        for i, v in enumerate(obj[:3]):
            results.extend(_find_lists(v, f"{path}[{i}]"))
    return results


def fetch_and_debug() -> List[Dict[str, Any]]:
    print("=" * 60, flush=True)
    print("STEP 1: IServ Login", flush=True)
    iserv = IServAPI(username=ISERV_USERNAME, password=ISERV_PASSWORD, iserv_url=ISERV_URL)
    print("  Login: OK", flush=True)
    session = iserv._session

    # --- Method A: get_emails() ---
    print("=" * 60, flush=True)
    print("STEP 2A: get_emails()", flush=True)
    try:
        raw_a = iserv.get_emails(path="INBOX", length=MAX_EMAILS, start=0, order="date", dir="desc")
        print(f"  Type: {type(raw_a).__name__}", flush=True)
        if isinstance(raw_a, dict):
            print(f"  Keys: {list(raw_a.keys())}", flush=True)
            for k, v in raw_a.items():
                content = json.dumps(v, ensure_ascii=False, default=str)
                if len(content) > 3000:
                    print(f"  {k} (first 3000 chars):", flush=True)
                    print(f"    {content[:3000]}", flush=True)
                else:
                    print(f"  {k}: {content}", flush=True)
            with open("debug_get_emails.json", "w", encoding="utf-8") as f:
                json.dump(raw_a, f, ensure_ascii=False, default=str, indent=2)
            lists = _find_lists(raw_a)
            if lists:
                print("  Lists found in structure:", flush=True)
                for path, length, item_type in lists:
                    print(f"    {path}: list of {length} {item_type}", flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        import traceback; traceback.print_exc()
        raw_a = None

    # --- Method B: Raw request with Accept: application/json ---
    print("=" * 60, flush=True)
    print("STEP 2B: Raw request with Accept: application/json", flush=True)
    mail_url = f"{{https://{ISERV_URL}}}/iserv/mail/api/message/list?path=INBOX&length={MAX_EMAILS}&start=0&order%5Bcolumn%5D=date&order%5Bdir%5D=desc"
    print(f"  URL: {mail_url}", flush=True)
    try:
        resp = session.get(mail_url, headers={"Accept": "application/json"})
        print(f"  Status: {resp.status_code}", flush=True)
        print(f"  Content-Type: {resp.headers.get('Content-Type', 'unknown')}", flush=True)
        print(f"  Length: {len(resp.text)} chars", flush=True)
        with open("debug_raw_json.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        try:
            raw_b = resp.json()
            print(f"  JSON parsed OK, type: {type(raw_b).__name__}", flush=True)
            if isinstance(raw_b, dict):
                print(f"  Keys: {list(raw_b.keys())}", flush=True)
                if "data" in raw_b and isinstance(raw_b["data"], list):
                    print(f"  data: {len(raw_b['data'])} items", flush=True)
                    if raw_b["data"] and isinstance(raw_b["data"][0], dict):
                        print(f"  data[0] keys: {list(raw_b['data'][0].keys())}", flush=True)
                        for k, v in raw_b["data"][0].items():
                            print(f"    {k}: {str(v)[:200]}", flush=True)
            elif isinstance(raw_b, list):
                print(f"  List length: {len(raw_b)}", flush=True)
                if raw_b and isinstance(raw_b[0], dict):
                    print(f"  [0] keys: {list(raw_b[0].keys())}", flush=True)
                    for k, v in raw_b[0].items():
                        print(f"    {k}: {str(v)[:200]}", flush=True)
            with open("debug_raw_json_parsed.json", "w", encoding="utf-8") as f:
                json.dump(raw_b, f, ensure_ascii=False, default=str, indent=2)
            if isinstance(raw_b, list): return raw_b
            elif isinstance(raw_b, dict) and "data" in raw_b and isinstance(raw_b["data"], list): return raw_b["data"]
        except Exception:
            print(f"  JSON parse failed", flush=True)
            print(f"  First 2000 chars: {resp.text[:2000]}", flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        import traceback; traceback.print_exc()

    # --- Method C: X-Requested-With header ---
    print("=" * 60, flush=True)
    print("STEP 2C: Try with X-Requested-With: XMLHttpRequest", flush=True)
    try:
        resp2 = session.get(mail_url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        print(f"  Status: {resp2.status_code}", flush=True)
        print(f"  Content-Type: {resp2.headers.get('Content-Type', 'unknown')}", flush=True)
        try:
            raw_c = resp2.json()
            print(f"  JSON parsed OK, type: {type(raw_c).__name__}", flush=True)
            if isinstance(raw_c, dict):
                print(f"  Keys: {list(raw_c.keys())}", flush=True)
                if "data" in raw_c and isinstance(raw_c["data"], list):
                    print(f"  data: {len(raw_c['data'])} items", flush=True)
                    if raw_c["data"] and isinstance(raw_c["data"][0], dict):
                        print(f"  data[0] keys: {list(raw_c['data'][0].keys())}", flush=True)
                        for k, v in raw_c["data"][0].items():
                            print(f"    {k}: {str(v)[:200]}", flush=True)
            elif isinstance(raw_c, list):
                print(f"  List length: {len(raw_c)}", flush=True)
                if raw_c and isinstance(raw_c[0], dict):
                    print(f"  [0] keys: {list(raw_c[0].keys())}", flush=True)
            with open("debug_xhr_parsed.json", "w", encoding="utf-8") as f:
                json.dump(raw_c, f, ensure_ascii=False, default=str, indent=2)
            if isinstance(raw_c, list): return raw_c
            elif isinstance(raw_c, dict) and "data" in raw_c and isinstance(raw_c["data"], list): return raw_c["data"]
        except Exception:
            print(f"  JSON parse failed", flush=True)
            print(f"  First 1000 chars: {resp2.text[:1000]}", flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)

    print("=" * 60, flush=True)
    print("  No email data extracted from any method", flush=True)
    return []


def _first(row, *keys):
    for k in keys:
        if k in row and row[k] is not None: return row[k]
    return None

def _strip_html(text):
    if text is None: return ""
    import re
    s = re.sub(r'<[^>]+>', '', str(text))
    return s.replace('&amp;','&').replace('&lt;','<').replace('&gt;','>').replace('&quot;','"').replace('&#39;',"'").strip()

def parse_email(raw):
    if not isinstance(raw, dict):
        return {"uid": None, "subject": "(Parse Error)", "sender": "Unbekannt", "date_iso": None, "is_read": False, "preview": ""}
    uid = _first(raw, "uid","UID","id","ID","message_id","messageId","DT_RowId","rowId","msg","number")
    subject = _first(raw, "subject","Subject","betreff","Betreff","title","Title","name","Name")
    sender = _first(raw, "from","From","from_name","sender","absender","fromEmail","from_email","senderEmail")
    date_val = _first(raw, "date","Date","date_timestamp","timestamp","time","Time","dateReceived","received")
    seen = _first(raw, "seen","read","is_read","gelesen","unread","isRead","wasRead","flags","flag")
    preview = _first(raw, "preview","snippet","excerpt","vorschau","text","body","content","previewText")
    if subject is None: subject = _first(raw, "1","2","0")
    if sender is None: sender = _first(raw, "2","3","1")
    if date_val is None: date_val = _first(raw, "3","4","5")
    subject = _strip_html(subject) if subject else "(Kein Betreff)"
    sender = _strip_html(sender) if sender else "Unbekannt"
    preview = _strip_html(preview) if preview else ""
    if not subject: subject = "(Kein Betreff)"
    if not sender: sender = "Unbekannt"
    date_iso = None
    if date_val is not None:
        if isinstance(date_val, (int, float)):
            date_iso = datetime.fromtimestamp(int(date_val)).isoformat()
        else:
            ds = _strip_html(date_val)
            for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M:%S","%d.%m.%Y %H:%M:%S","%Y-%m-%d %H:%M","%d.%m.%Y","%Y-%m-%d"):
                try: date_iso = datetime.strptime(ds, fmt).isoformat(); break
                except ValueError: continue
            if date_iso is None:
                try: date_iso = datetime.fromisoformat(ds).isoformat()
                except: date_iso = datetime.now().isoformat()
    if isinstance(seen, bool): is_read = seen
    elif isinstance(seen, str): is_read = seen.lower() in ("true","1","yes","gelesen","read")
    elif isinstance(seen, (int, float)): is_read = bool(seen)
    else: is_read = False
    if preview and len(preview) > 200: preview = preview[:200] + "..."
    uid_str = str(uid).strip() if uid is not None else None
    return {"uid": uid_str, "subject": subject, "sender": sender, "date_iso": date_iso, "is_read": is_read, "preview": preview}

def notion_headers():
    return {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

def get_existing_uids():
    existing = set()
    cursor = None
    has_more = True
    while has_more:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        resp = requests.post(f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}/query", headers=notion_headers(), json=body)
        if resp.status_code != 200: break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            rt = props.get("IServ UID", {}).get("rich_text", [])
            if rt: existing.add(rt[0].get("plain_text", ""))
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return existing

def create_notion_page(email):
    properties = {
        "Betreff": {"title": [{"text": {"content": email["subject"][:2000]}}]},
        "Absender": {"rich_text": [{"text": {"content": email["sender"][:2000]}}]},
        "Gelesen": {"checkbox": email["is_read"]},
        "Vorschau": {"rich_text": [{"text": {"content": email["preview"][:2000]}}]},
        "Ordner": {"select": {"name": "Posteingang"}},
    }
    if email["date_iso"]: properties["Datum"] = {"date": {"start": email["date_iso"]}}
    if email["uid"]: properties["IServ UID"] = {"rich_text": [{"text": {"content": email["uid"]}}]}
    resp = requests.post(f"{NOTION_API_URL}/pages", headers=notion_headers(), json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties})
    if resp.status_code == 200:
        print(f"  Created: {email['subject'][:60]}", flush=True)
        return True
    else:
        print(f"  Failed ({resp.status_code}): {resp.text[:200]}", flush=True)
        return False

def main():
    print("=" * 60, flush=True)
    print("IServ -> Notion Sync — DEBUG BUILD v4", flush=True)
    print("=" * 60, flush=True)
    validate_config()
    try:
        raw_emails = fetch_and_debug()
    except Exception as exc:
        print(f"FATAL: {exc}", flush=True)
        import traceback; traceback.print_exc()
        sys.exit(1)
    if not raw_emails:
        print("No emails to sync.", flush=True)
        return
    parsed = []
    for raw in raw_emails:
        try: parsed.append(parse_email(raw))
        except Exception as exc: print(f"Parse error: {exc}", flush=True)
    print(f"\nParsed {len(parsed)} emails", flush=True)
    for i, email in enumerate(parsed[:3]):
        print(f"  Email {i}: subject='{email['subject'][:50]}' sender='{email['sender'][:50]}' uid='{email['uid']}'", flush=True)
    existing = get_existing_uids()
    new = skip = err = 0
    for email in parsed:
        if email["uid"] and email["uid"] in existing: skip += 1; continue
        try:
            if create_notion_page(email): new += 1
            else: err += 1
        except Exception as exc:
            print(f"Create failed: {exc}", flush=True); err += 1
        time.sleep(RATE_LIMIT_SEC)
    print(f"\nSync: {new} new | {skip} skipped | {err} errors", flush=True)

if __name__ == "__main__":
    main()
