#!/usr/bin/env python3
"""
IServ to Notion Sync — DEBUG BUILD v5
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


def build_mail_url(extra_params=""):
    """Build mail API URL using string concatenation to avoid f-string brace issues."""
    base = "https://" + ISERV_URL + "/iserv/mail/api/message/list?path=INBOX&length=" + str(MAX_EMAILS) + "&start=0&order%5Bcolumn%5D=date&order%5Bdir%5D=desc"
    if extra_params:
        base += "&" + extra_params
    return base


def fetch_and_debug() -> List[Dict[str, Any]]:
    print("=" * 60, flush=True)
    print("STEP 1: IServ Login", flush=True)
    iserv = IServAPI(username=ISERV_USERNAME, password=ISERV_PASSWORD, iserv_url=ISERV_URL)
    print("  Login: OK", flush=True)
    session = iserv._session

    mail_url = build_mail_url()
    print(f"  Mail URL: {mail_url}", flush=True)

    # --- Method A: Accept: application/json ---
    print("=" * 60, flush=True)
    print("STEP 2A: Raw request with Accept: application/json", flush=True)
    try:
        resp = session.get(mail_url, headers={"Accept": "application/json"})
        print(f"  Status: {resp.status_code}", flush=True)
        print(f"  Content-Type: {resp.headers.get('Content-Type', 'unknown')}", flush=True)
        print(f"  Length: {len(resp.text)} chars", flush=True)
        with open("debug_resp_a.txt", "w", encoding="utf-8") as f:
            f.write(resp.text)
        try:
            data = resp.json()
            print(f"  JSON OK! Type: {type(data).__name__}", flush=True)
            return _extract_and_log(data, "2A")
        except Exception:
            print(f"  Not JSON. First 2000 chars:", flush=True)
            print(resp.text[:2000], flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        import traceback; traceback.print_exc()

    # --- Method B: X-Requested-With: XMLHttpRequest ---
    print("=" * 60, flush=True)
    print("STEP 2B: Raw request with X-Requested-With + Accept:json", flush=True)
    try:
        resp = session.get(mail_url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        print(f"  Status: {resp.status_code}", flush=True)
        print(f"  Content-Type: {resp.headers.get('Content-Type', 'unknown')}", flush=True)
        print(f"  Length: {len(resp.text)} chars", flush=True)
        with open("debug_resp_b.txt", "w", encoding="utf-8") as f:
            f.write(resp.text)
        try:
            data = resp.json()
            print(f"  JSON OK! Type: {type(data).__name__}", flush=True)
            return _extract_and_log(data, "2B")
        except Exception:
            print(f"  Not JSON. First 2000 chars:", flush=True)
            print(resp.text[:2000], flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        import traceback; traceback.print_exc()

    # --- Method C: DataTables-style params ---
    print("=" * 60, flush=True)
    print("STEP 2C: DataTables-style request", flush=True)
    dt_url = build_mail_url("draw=1")
    try:
        resp = session.get(dt_url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        print(f"  Status: {resp.status_code}", flush=True)
        print(f"  Content-Type: {resp.headers.get('Content-Type', 'unknown')}", flush=True)
        print(f"  Length: {len(resp.text)} chars", flush=True)
        with open("debug_resp_c.txt", "w", encoding="utf-8") as f:
            f.write(resp.text)
        try:
            data = resp.json()
            print(f"  JSON OK! Type: {type(data).__name__}", flush=True)
            return _extract_and_log(data, "2C")
        except Exception:
            print(f"  Not JSON. First 2000 chars:", flush=True)
            print(resp.text[:2000], flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        import traceback; traceback.print_exc()

    print("=" * 60, flush=True)
    print("  No email data extracted", flush=True)
    return []


def _extract_and_log(data: Any, label: str) -> List[Dict[str, Any]]:
    """Extract email list from response and log structure."""
    with open("debug_data_" + label + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str, indent=2)

    if isinstance(data, dict):
        print(f"  Dict keys: {list(data.keys())}", flush=True)
        for k, v in data.items():
            if isinstance(v, list) and v:
                print(f"  {k}: list of {len(v)} {type(v[0]).__name__}", flush=True)
                if isinstance(v[0], dict):
                    print(f"  {k}[0] keys: {list(v[0].keys())}", flush=True)
                    for ik, iv in v[0].items():
                        print(f"    {ik}: {str(iv)[:200]}", flush=True)
            elif isinstance(v, dict):
                print(f"  {k}: dict (keys: {list(v.keys())[:10]})", flush=True)
            else:
                print(f"  {k}: {str(v)[:200]}", flush=True)
        if "data" in data and isinstance(data["data"], list):
            return data["data"]
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"  Using '{k}' as email list", flush=True)
                return v
    elif isinstance(data, list):
        print(f"  List length: {len(data)}", flush=True)
        if data and isinstance(data[0], dict):
            print(f"  [0] keys: {list(data[0].keys())}", flush=True)
            for k, v in data[0].items():
                print(f"    {k}: {str(v)[:200]}", flush=True)
        return data

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
    return {"Authorization": "Bearer " + NOTION_TOKEN, "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

def get_existing_uids():
    existing = set()
    cursor = None
    has_more = True
    while has_more:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        resp = requests.post(NOTION_API_URL + "/databases/" + NOTION_DATABASE_ID + "/query", headers=notion_headers(), json=body)
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
    resp = requests.post(NOTION_API_URL + "/pages", headers=notion_headers(), json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties})
    if resp.status_code == 200:
        print(f"  Created: {email['subject'][:60]}", flush=True)
        return True
    else:
        print(f"  Failed ({resp.status_code}): {resp.text[:200]}", flush=True)
        return False

def main():
    print("=" * 60, flush=True)
    print("IServ -> Notion Sync — DEBUG BUILD v5", flush=True)
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
