#!/usr/bin/env python3
"""
gdrive_triage.py - Interactive Google Drive Migration & Triage Assistant (Direct Google Drive API v3)

Features:
- Uses the Google Drive REST API v3 directly (https://www.googleapis.com/drive/v3/)
  instead of crawling folders via rclone/rsync:
  * Single metadata query (`trashed = false and 'me' in owners and mimeType in (...)`)
    discovers 100% of all Google-native documents (Google Sheets, Google Docs,
    Google Slides, Google Drawings) across the entire Google Drive regardless of folder depth.
  * Strictly filters to files OWNED BY THE USER (`'me' in owners` / `ownedByMe == true`),
    automatically hiding all "Shared with me" documents owned by other accounts.
  * Resolves and caches the complete Google Drive folder & subfolder hierarchy in SQLite
    (`drive_folders` table), exposing both top-level folders and nested subfolder paths.
  * Converts/exports documents on-demand directly via Google Drive REST API v3
    (`GET /drive/v3/files/{fileId}/export?mimeType=...`) and uploads to Collabora Online
    (via miniwopi `POST /upload`) or archives to NAS.
  * Deletes (moves to Google Drive Trash) directly by File ID via Google Drive REST API v3
    (`PATCH /drive/v3/files/{fileId}` with `{"trashed": true}`).
  * Includes a built-in 2-click Web UI OAuth Scope Upgrade flow (`/api/oauth/start` &
    `/api/oauth/complete`) to upgrade an existing `drive.readonly` token to full `drive`
    write/delete permission directly from the browser.
"""

import argparse
import configparser
import datetime
import html
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# Force IPv4 for urllib connections so Google API calls never stall on broken IPv6 routes
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/gdrive_triage/gdrive_triage.db")
RCLONE_CONF_PATH = os.path.expanduser("~/.config/rclone/rclone.conf")
RCLONE_SECTION = os.environ.get("GDRIVE_RCLONE_SECTION", "gdrive")
DEFAULT_PORT = 8085
COLLABORA_WEB_URL = os.environ.get("COLLABORA_URL", "http://collabora").rstrip("/")
NAS_ARCHIVE_DIR = os.environ.get("NAS_ARCHIVE_DIR", os.path.expanduser("~/archive/google_drive"))

GOOGLE_NATIVE_MIMES = {
    "application/vnd.google-apps.spreadsheet": (
        "spreadsheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.document": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.presentation": (
        "presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": (
        "drawing",
        "image/svg+xml",
        ".svg",
    ),
}

# In-memory OAuth token & scope state
_TOKEN_LOCK = threading.Lock()
_CACHED_ACCESS_TOKEN: Optional[str] = None
_CACHED_TOKEN_EXPIRY: float = 0.0
_CACHED_SCOPE_INFO: Optional[Dict[str, Any]] = None

# Background `rclone authorize` process for the 2-click Web UI scope upgrade
_AUTH_PROC_LOCK = threading.Lock()
_AUTH_PROC: Optional[subprocess.Popen] = None
_AUTH_LOCAL_URL: Optional[str] = None
_AUTH_GOOGLE_URL: Optional[str] = None


# ----------------------------------------------------------------------
# DATABASE LAYER
# ----------------------------------------------------------------------

def get_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                folder TEXT NOT NULL,
                top_folder TEXT NOT NULL DEFAULT 'root',
                is_dir INTEGER NOT NULL DEFAULT 0,
                mime_type TEXT,
                size INTEGER DEFAULT -1,
                mod_time TEXT,
                category TEXT,
                triage_status TEXT DEFAULT 'pending',
                target_path TEXT,
                notes TEXT,
                drive_deleted INTEGER NOT NULL DEFAULT 0,
                owned_by_me INTEGER NOT NULL DEFAULT 1,
                owner_email TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "drive_deleted" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN drive_deleted INTEGER NOT NULL DEFAULT 0")
        if "top_folder" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN top_folder TEXT NOT NULL DEFAULT 'root'")
        if "owned_by_me" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN owned_by_me INTEGER NOT NULL DEFAULT 1")
        if "owner_email" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN owner_email TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_folder ON items(folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_top_folder ON items(top_folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON items(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON items(triage_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON items(name)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS drive_folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id TEXT,
                full_path TEXT,
                owned_by_me INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                name TEXT PRIMARY KEY,
                last_scanned TIMESTAMP,
                item_count INTEGER DEFAULT 0
            )
        """)

        # Strictly keep only user-owned Google-native documents (size = -1, is_dir = 0, owned_by_me = 1)
        conn.execute("DELETE FROM items WHERE size != -1 OR is_dir != 0 OR owned_by_me = 0")
    return conn


# ----------------------------------------------------------------------
# DIRECT GOOGLE DRIVE REST API v3 & OAUTH MANAGEMENT
# ----------------------------------------------------------------------

def _parse_iso_expiry(expiry_str: str) -> float:
    if not expiry_str:
        return 0.0
    try:
        # Handle nanoseconds in Go/rclone timestamps like 2026-11-21T03:30:35.803597087+01:00
        cleaned = re.sub(r"(\.\d{6})\d+", r"\1", expiry_str)
        dt = datetime.datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except Exception:
        return 0.0


def _read_rclone_token_config() -> Tuple[str, Dict[str, Any]]:
    cp = configparser.ConfigParser()
    cp.read(RCLONE_CONF_PATH)
    if RCLONE_SECTION not in cp:
        raise RuntimeError(f"Section [{RCLONE_SECTION}] not found in {RCLONE_CONF_PATH}")
    scope = cp[RCLONE_SECTION].get("scope", "drive.readonly").strip()
    raw_token = cp[RCLONE_SECTION].get("token", "{}")
    tok_data = json.loads(raw_token)
    return scope, tok_data


def refresh_access_token(force: bool = False) -> str:
    """Return a valid Google OAuth 2.0 access token, refreshing via rclone if near expiry."""
    global _CACHED_ACCESS_TOKEN, _CACHED_TOKEN_EXPIRY, _CACHED_SCOPE_INFO
    with _TOKEN_LOCK:
        now = time.time()
        if not force and _CACHED_ACCESS_TOKEN and now < (_CACHED_TOKEN_EXPIRY - 180):
            return _CACHED_ACCESS_TOKEN

        _, tok_data = _read_rclone_token_config()
        expiry_ts = _parse_iso_expiry(tok_data.get("expiry", ""))
        if force or not tok_data.get("access_token") or now >= (expiry_ts - 180):
            # Trigger rclone token refresh so rclone updates ~/.config/rclone/rclone.conf
            subprocess.run(
                ["rclone", "--bind", "0.0.0.0", "about", f"{RCLONE_SECTION}:"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            _, tok_data = _read_rclone_token_config()
            expiry_ts = _parse_iso_expiry(tok_data.get("expiry", ""))

        new_tok = tok_data.get("access_token", "")
        if new_tok != _CACHED_ACCESS_TOKEN:
            _CACHED_SCOPE_INFO = None
        _CACHED_ACCESS_TOKEN = new_tok
        _CACHED_TOKEN_EXPIRY = expiry_ts if expiry_ts > now else (now + 1800)
        return _CACHED_ACCESS_TOKEN


def get_oauth_scope_status(force_check: bool = False) -> Dict[str, Any]:
    """Inspect active OAuth token scopes against Google's tokeninfo endpoint."""
    global _CACHED_SCOPE_INFO
    if not force_check and _CACHED_SCOPE_INFO is not None:
        return _CACHED_SCOPE_INFO

    try:
        conf_scope, _ = _read_rclone_token_config()
    except Exception as exc:
        return {
            "ok": False,
            "configured_scope": "unknown",
            "live_scopes": "",
            "can_delete": False,
            "error": str(exc),
        }

    try:
        token = refresh_access_token(force=False)
        url = f"https://oauth2.googleapis.com/tokeninfo?access_token={urllib.parse.quote(token)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        live_scopes = info.get("scope", "")
        scope_list = live_scopes.split()
        can_delete = "https://www.googleapis.com/auth/drive" in scope_list
        _CACHED_SCOPE_INFO = {
            "ok": True,
            "configured_scope": conf_scope,
            "live_scopes": live_scopes,
            "can_delete": can_delete,
        }
        return _CACHED_SCOPE_INFO
    except Exception as exc:
        can_delete = conf_scope == "drive"
        return {
            "ok": False,
            "configured_scope": conf_scope,
            "live_scopes": "",
            "can_delete": can_delete,
            "error": str(exc),
        }


def gdrive_api_request(
    url: str,
    method: str = "GET",
    json_body: Optional[Dict[str, Any]] = None,
    raw_bytes: bool = False,
    max_attempts: int = 12,
) -> Any:
    """Execute a direct Google Drive REST API v3 request with automatic token refresh & rate-limit pacer."""
    token = refresh_access_token(force=False)
    body_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else None

    for attempt in range(max_attempts):
        headers = {"Authorization": f"Bearer {token}"}
        if body_bytes is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                if raw_bytes:
                    return raw
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and attempt == 0:
                token = refresh_access_token(force=True)
                continue
            if exc.code in (403, 429, 500, 502, 503, 504):
                # Do NOT retry if it is an insufficient OAuth scope or file permission error
                if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in err_text or "insufficientPermissions" in err_text:
                    raise RuntimeError(
                        "HTTP 403 Insufficient OAuth Scope (ACCESS_TOKEN_SCOPE_INSUFFICIENT): "
                        "Current token has 'drive.readonly' scope. Click '🔑 Authorize Delete Access' "
                        "in the top bar to grant Google Drive write/delete permission."
                    ) from exc
                if "insufficientFilePermissions" in err_text:
                    raise RuntimeError(f"HTTP 403 Insufficient File Permissions: {err_text}") from exc
                if "exportSizeLimitExceeded" in err_text:
                    raise RuntimeError("EXPORT_SIZE_LIMIT_EXCEEDED") from exc
                # Otherwise it's a transient rate limit on the shared client_id; pace and retry
                time.sleep(min(0.6 + 0.45 * attempt, 4.0))
                continue
            raise RuntimeError(f"Google Drive API HTTP {exc.code}: {err_text}") from exc
    raise RuntimeError(f"Google Drive API request failed after {max_attempts} attempts: {url}")


def start_oauth_upgrade_session() -> Tuple[bool, str]:
    """Start `rclone authorize drive --auth-no-open-browser` and return the Google OAuth consent URL."""
    global _AUTH_PROC, _AUTH_LOCAL_URL, _AUTH_GOOGLE_URL
    with _AUTH_PROC_LOCK:
        if _AUTH_PROC is not None and _AUTH_PROC.poll() is None and _AUTH_GOOGLE_URL:
            return True, _AUTH_GOOGLE_URL

        if _AUTH_PROC is not None and _AUTH_PROC.poll() is None:
            try:
                _AUTH_PROC.terminate()
                _AUTH_PROC.wait(timeout=2)
            except Exception:
                pass

        # Kill any stale rclone authorize listening on 53682
        subprocess.run(["pkill", "-f", "rclone.*authorize.*drive"], capture_output=True)

        proc = subprocess.Popen(
            ["rclone", "--bind", "0.0.0.0", "authorize", "drive", "--auth-no-open-browser"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        local_url = None
        t0 = time.time()
        while time.time() - t0 < 6.0:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            m = re.search(r"(http://127\.0\.0\.1:\d+/auth\?state=\S+)", line)
            if m:
                local_url = m.group(1)
                break

        if not local_url:
            proc.terminate()
            return False, "Failed to start local rclone OAuth listener on port 53682."

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        google_url = None
        try:
            opener.open(local_url, timeout=5)
        except urllib.error.HTTPError as exc:
            google_url = exc.headers.get("Location")

        if not google_url:
            proc.terminate()
            return False, "Failed to obtain Google OAuth 2.0 redirect URL from rclone listener."

        _AUTH_PROC = proc
        _AUTH_LOCAL_URL = local_url
        _AUTH_GOOGLE_URL = google_url
        return True, google_url


def complete_oauth_upgrade_session(redirected_url_or_code: str) -> Tuple[bool, str]:
    """Forward the redirected `http://127.0.0.1:53682/?state=...&code=...` callback to rclone and save the new token."""
    global _AUTH_PROC, _AUTH_LOCAL_URL, _AUTH_GOOGLE_URL, _CACHED_ACCESS_TOKEN, _CACHED_SCOPE_INFO
    raw = (redirected_url_or_code or "").strip()
    if not raw:
        return False, "Please paste the redirected http://127.0.0.1:53682/?state=...&code=... URL from your browser."

    with _AUTH_PROC_LOCK:
        if _AUTH_PROC is None or _AUTH_PROC.poll() is not None:
            return False, "No active authorization session found. Please click '1. Open Google Permission Page' first."

        # Construct local callback URL on 127.0.0.1:53682
        if raw.startswith("http://127.0.0.1:") or raw.startswith("http://localhost:"):
            parsed = urllib.parse.urlparse(raw)
            callback_url = f"http://127.0.0.1:53682/{parsed.path.lstrip('/')}?{parsed.query}"
        elif "code=" in raw:
            qs = raw.split("?", 1)[-1]
            callback_url = f"http://127.0.0.1:53682/?{qs}"
        else:
            # User pasted just the raw code; extract state from _AUTH_LOCAL_URL
            state = ""
            if _AUTH_LOCAL_URL and "state=" in _AUTH_LOCAL_URL:
                state = urllib.parse.parse_qs(urllib.parse.urlparse(_AUTH_LOCAL_URL).query).get("state", [""])[0]
            callback_url = f"http://127.0.0.1:53682/?state={urllib.parse.quote(state)}&code={urllib.parse.quote(raw)}"

        try:
            with urllib.request.urlopen(callback_url, timeout=15) as resp:
                _ = resp.read()
        except Exception as exc:
            return False, f"Failed to send authorization code to local listener: {exc}"

        try:
            out, _ = _AUTH_PROC.communicate(timeout=15)
        except Exception as exc:
            return False, f"Timed out reading token from rclone authorize: {exc}"
        finally:
            _AUTH_PROC = None
            _AUTH_LOCAL_URL = None
            _AUTH_GOOGLE_URL = None

    m = re.search(r"--->\s*(\{.*?\})\s*<---End paste", out, re.DOTALL)
    if not m:
        return False, f"Could not parse OAuth token from rclone output: {out[-300:]}"

    new_token_str = m.group(1).strip()
    try:
        tok_obj = json.loads(new_token_str)
        if not tok_obj.get("access_token"):
            return False, "Returned token JSON is missing access_token."
    except Exception as exc:
        return False, f"Invalid token JSON returned: {exc}"

    # Update ~/.config/rclone/rclone.conf with scope = drive and new token
    cp = configparser.ConfigParser()
    cp.read(RCLONE_CONF_PATH)
    if RCLONE_SECTION not in cp:
        cp.add_section(RCLONE_SECTION)
    cp[RCLONE_SECTION]["type"] = "drive"
    cp[RCLONE_SECTION]["scope"] = "drive"
    cp[RCLONE_SECTION]["token"] = new_token_str

    with open(RCLONE_CONF_PATH, "w") as f:
        cp.write(f)

    with _TOKEN_LOCK:
        _CACHED_ACCESS_TOKEN = tok_obj["access_token"]
        _CACHED_TOKEN_EXPIRY = _parse_iso_expiry(tok_obj.get("expiry", "")) or (time.time() + 3500)
        _CACHED_SCOPE_INFO = None

    scope_status = get_oauth_scope_status(force_check=True)
    if scope_status.get("can_delete"):
        return True, "Google Drive OAuth token upgraded to full 'drive' write/delete scope!"
    return True, "OAuth token updated in rclone.conf."


# ----------------------------------------------------------------------
# DIRECT GOOGLE DRIVE API v3 INDEXER & FOLDER TREE RESOLVER
# ----------------------------------------------------------------------

def categorize_item(name: str, mime: str) -> str:
    if mime in GOOGLE_NATIVE_MIMES:
        return GOOGLE_NATIVE_MIMES[mime][0]
    lower_name = name.lower()
    lower_mime = (mime or "").lower()
    if "spreadsheet" in lower_mime or lower_name.endswith((".xlsx", ".xls", ".ods", ".csv")):
        return "spreadsheet"
    if "presentation" in lower_mime or "powerpoint" in lower_mime or lower_name.endswith((".pptx", ".ppt", ".odp")):
        return "presentation"
    if "document" in lower_mime or "word" in lower_mime or lower_name.endswith((".docx", ".doc", ".odt", ".rtf", ".txt")):
        return "document"
    if "drawing" in lower_mime or "svg" in lower_mime or lower_name.endswith(".svg"):
        return "drawing"
    return "other"


def ensure_office_extension(name: str, category: str) -> str:
    lower = name.lower()
    if category == "spreadsheet" and not lower.endswith((".xlsx", ".xls", ".ods", ".csv")):
        return name + ".xlsx"
    if category == "document" and not lower.endswith((".docx", ".doc", ".odt", ".rtf", ".txt")):
        return name + ".docx"
    if category == "presentation" and not lower.endswith((".pptx", ".ppt", ".odp")):
        return name + ".pptx"
    if category == "drawing" and not lower.endswith(".svg"):
        return name + ".svg"
    return name


def _load_folder_cache_from_db(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute("SELECT id, name, parent_id, full_path, owned_by_me FROM drive_folders").fetchall()
    cache: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cache[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "parents": [r["parent_id"]] if r["parent_id"] else [],
            "full_path": r["full_path"],
            "ownedByMe": bool(r["owned_by_me"]),
        }
    return cache


def _resolve_missing_folders(
    conn: sqlite3.Connection,
    parent_ids: set,
    root_id: str,
    seed_cache_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Resolve folder metadata up to root_id and persist in SQLite `drive_folders`."""
    folder_cache = _load_folder_cache_from_db(conn)

    # Import any pre-resolved folders from /tmp/folder_cache.json if present
    if seed_cache_path and os.path.exists(seed_cache_path):
        try:
            with open(seed_cache_path) as f:
                seed_data = json.load(f)
            for fid, meta in seed_data.items():
                if meta and fid not in folder_cache:
                    folder_cache[fid] = meta
        except Exception:
            pass

    pending = {pid for pid in parent_ids if pid and pid != root_id and pid not in folder_cache}

    def fetch_folder(fid: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        url = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(fid)}?fields=id,name,parents,ownedByMe"
        try:
            return fid, gdrive_api_request(url)
        except Exception:
            return fid, None

    while pending:
        batch = list(pending)
        pending.clear()
        with ThreadPoolExecutor(max_workers=4) as ex:
            for fid, meta in ex.map(fetch_folder, batch):
                if meta:
                    folder_cache[fid] = meta
                    for p in meta.get("parents", []):
                        if p and p != root_id and p not in folder_cache:
                            pending.add(p)

    # Compute full_path for every cached folder and store in SQLite
    def build_path(fid: str) -> str:
        parts = []
        curr = fid
        seen = set()
        while curr and curr != root_id and curr not in seen:
            seen.add(curr)
            meta = folder_cache.get(curr)
            if not meta:
                break
            parts.append(meta["name"])
            parents = meta.get("parents") or []
            curr = parents[0] if parents else None
        parts.reverse()
        return "/".join(parts)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        for fid, meta in folder_cache.items():
            if not meta or fid == root_id or meta.get("parents") == ["__ROOT__"]:
                continue
            full_path = build_path(fid)
            meta["full_path"] = full_path
            parents = meta.get("parents") or []
            parent_id = parents[0] if parents else None
            owned = 1 if meta.get("ownedByMe", True) else 0
            conn.execute("""
                INSERT INTO drive_folders (id, name, parent_id, full_path, owned_by_me, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    parent_id = excluded.parent_id,
                    full_path = excluded.full_path,
                    owned_by_me = excluded.owned_by_me,
                    updated_at = excluded.updated_at
            """, (fid, meta["name"], parent_id, full_path, owned, now_iso))

    return folder_cache


def index_all_via_api(
    db_path: str = DEFAULT_DB_PATH,
    seed_docs_path: Optional[str] = None,
) -> int:
    """
    Query Google Drive REST API v3 directly for ALL user-owned Google-native documents
    (`trashed = false and 'me' in owners`) across the entire Google Drive.
    """
    conn = get_db(db_path)
    t0 = time.time()
    print("Indexing all user-owned Google-native documents via Google Drive REST API v3...")

    root_id = "root"
    cached_root = conn.execute("SELECT id FROM drive_folders WHERE parent_id = '__ROOT__' LIMIT 1").fetchone()
    if cached_root:
        root_id = cached_root["id"]
    owned_docs: List[Dict[str, Any]] = []

    if seed_docs_path and os.path.exists(seed_docs_path):
        with open(seed_docs_path) as f:
            owned_docs = json.load(f)
    else:
        if root_id == "root":
            try:
                root_meta = gdrive_api_request("https://www.googleapis.com/drive/v3/files/root?fields=id")
                if root_meta.get("id"):
                    root_id = root_meta["id"]
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO drive_folders (id, name, parent_id, full_path, owned_by_me) VALUES (?, 'My Drive', '__ROOT__', 'root', 1)",
                            (root_id,),
                        )
            except Exception:
                pass

        mime_clauses = " or ".join(f"mimeType = '{m}'" for m in GOOGLE_NATIVE_MIMES.keys())
        q = f"trashed = false and 'me' in owners and ({mime_clauses})"
        page_token: Optional[str] = None
        while True:
            params = {
                "q": q,
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,parents,ownedByMe,owners(displayName,emailAddress),shared,modifiedTime)",
            }
            if page_token:
                params["pageToken"] = page_token
            url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
            data = gdrive_api_request(url)
            for f in data.get("files", []):
                # Double-check strict user ownership
                if f.get("ownedByMe", False):
                    owned_docs.append(f)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    parent_ids = {p for doc in owned_docs for p in (doc.get("parents") or []) if p and p != root_id}
    folder_cache = _resolve_missing_folders(conn, parent_ids, root_id)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    seen_ids = set()
    folder_counts: Dict[str, int] = {}

    with conn:
        for doc in owned_docs:
            if not doc.get("ownedByMe", False):
                continue
            item_id = doc.get("id")
            if not item_id:
                continue
            seen_ids.add(item_id)

            raw_name = doc.get("name", "Untitled")
            mime = doc.get("mimeType", "")
            category = categorize_item(raw_name, mime)
            export_name = ensure_office_extension(raw_name, category)

            parents = doc.get("parents") or []
            parent_id = parents[0] if parents else None
            if not parent_id or parent_id == root_id:
                full_folder = "root"
                top_folder = "root"
            else:
                fmeta = folder_cache.get(parent_id)
                resolved_path = (fmeta.get("full_path") if fmeta else "") or ""
                if resolved_path:
                    full_folder = resolved_path
                    top_folder = resolved_path.split("/")[0]
                else:
                    full_folder = "root"
                    top_folder = "root"

            full_path = export_name if full_folder == "root" else f"{full_folder}/{export_name}"
            mod_time = doc.get("modifiedTime", "")
            owners = doc.get("owners") or []
            owner_email = owners[0].get("emailAddress", "") if owners else ""

            existing = conn.execute(
                "SELECT triage_status, target_path, notes, drive_deleted FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            triage_status = existing["triage_status"] if existing else "pending"
            target_path = existing["target_path"] if existing else None
            notes = existing["notes"] if existing else None
            drive_deleted = existing["drive_deleted"] if existing else 0

            conn.execute("""
                INSERT INTO items (
                    id, path, name, folder, top_folder, is_dir, mime_type, size, mod_time,
                    category, triage_status, target_path, notes, drive_deleted,
                    owned_by_me, owner_email, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, -1, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    name = excluded.name,
                    folder = excluded.folder,
                    top_folder = excluded.top_folder,
                    is_dir = 0,
                    mime_type = excluded.mime_type,
                    size = -1,
                    mod_time = excluded.mod_time,
                    category = excluded.category,
                    owned_by_me = 1,
                    owner_email = excluded.owner_email,
                    updated_at = excluded.updated_at
            """, (
                item_id,
                full_path,
                export_name,
                full_folder,
                top_folder,
                mime,
                mod_time,
                category,
                triage_status,
                target_path,
                notes,
                drive_deleted,
                owner_email,
                now_iso,
            ))
            folder_counts[full_folder] = folder_counts.get(full_folder, 0) + 1

        # Purge any pending items in DB that are NOT in seen_ids (e.g. non-owned shared files or already trashed files)
        all_db_rows = conn.execute("SELECT id, triage_status, drive_deleted FROM items").fetchall()
        for r in all_db_rows:
            if r["id"] not in seen_ids:
                # If the user previously converted it to Collabora/NAS and then deleted it from Drive, keep its record marked drive_deleted=1
                if r["triage_status"] in ("imported_collabora", "archived_nas", "deleted") or r["drive_deleted"] == 1:
                    conn.execute("UPDATE items SET drive_deleted = 1 WHERE id = ?", (r["id"],))
                else:
                    conn.execute("DELETE FROM items WHERE id = ?", (r["id"],))

        conn.execute("DELETE FROM folders")
        for fname, fcnt in folder_counts.items():
            conn.execute(
                "INSERT INTO folders (name, last_scanned, item_count) VALUES (?, ?, ?)",
                (fname, now_iso, fcnt),
            )

    elapsed = time.time() - t0
    print(f"Indexed {len(seen_ids)} owned Google-native documents across {len(folder_counts)} folder paths in {elapsed:.2f}s.")
    return len(seen_ids)


def index_folder(folder_name: str = "all", db_path: str = DEFAULT_DB_PATH, max_depth: int = 10) -> int:
    """In the direct Google Drive API v3 architecture, indexing always refreshes all user-owned docs in 1 query."""
    return index_all_via_api(db_path=db_path)


# ----------------------------------------------------------------------
# QUERY & SUMMARY HELPERS
# ----------------------------------------------------------------------

def get_folders_summary(db_path: str = DEFAULT_DB_PATH) -> Dict[str, Any]:
    """Return both top-level folder summaries and full subfolder path summaries."""
    conn = get_db(db_path)
    sub_rows = conn.execute("""
        SELECT folder,
               top_folder,
               COUNT(*) as total,
               SUM(CASE WHEN triage_status = 'pending' AND drive_deleted = 0 THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN triage_status = 'imported_collabora' THEN 1 ELSE 0 END) as imported,
               SUM(CASE WHEN triage_status = 'archived_nas' THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN triage_status = 'deleted' OR drive_deleted = 1 THEN 1 ELSE 0 END) as deleted
        FROM items
        WHERE is_dir = 0 AND size = -1 AND owned_by_me = 1
        GROUP BY folder, top_folder
        HAVING total > 0
        ORDER BY top_folder ASC, folder ASC
    """).fetchall()

    top_rows = conn.execute("""
        SELECT top_folder,
               COUNT(*) as total,
               SUM(CASE WHEN triage_status = 'pending' AND drive_deleted = 0 THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN triage_status = 'imported_collabora' THEN 1 ELSE 0 END) as imported,
               SUM(CASE WHEN triage_status = 'archived_nas' THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN triage_status = 'deleted' OR drive_deleted = 1 THEN 1 ELSE 0 END) as deleted,
               COUNT(DISTINCT folder) as subfolder_count
        FROM items
        WHERE is_dir = 0 AND size = -1 AND owned_by_me = 1
        GROUP BY top_folder
        HAVING total > 0
        ORDER BY pending DESC, total DESC
    """).fetchall()

    return {
        "top_folders": [dict(r) for r in top_rows],
        "subfolders": [dict(r) for r in sub_rows],
    }


def get_items(
    db_path: str = DEFAULT_DB_PATH,
    folder: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    clauses = ["is_dir = 0", "size = -1", "owned_by_me = 1"]
    params: List[Any] = []

    if folder and folder != "all":
        if folder.startswith("top:"):
            top_name = folder[len("top:"):]
            clauses.append("top_folder = ?")
            params.append(top_name)
        else:
            clauses.append("folder = ?")
            params.append(folder)

    if category and category != "all":
        clauses.append("category = ?")
        params.append(category)

    if status and status != "all":
        if status == "deleted":
            clauses.append("(triage_status = 'deleted' OR drive_deleted = 1)")
        elif status == "pending":
            clauses.append("(triage_status = 'pending' AND drive_deleted = 0)")
        else:
            clauses.append("triage_status = ?")
            params.append(status)

    if query:
        clauses.append("(name LIKE ? OR path LIKE ? OR folder LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])

    sql = f"SELECT * FROM items WHERE {' AND '.join(clauses)} ORDER BY mod_time DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["collabora_edit_url"] = build_collabora_edit_url(d)
        result.append(d)
    return result


def build_collabora_edit_url(item: Dict[str, Any]) -> str:
    """Build direct Collabora /edit/<encoded_relpath> URL for an item."""
    rel_target = item.get("target_path") or ""
    if rel_target.startswith("/srv/spreadsheets/"):
        rel_target = rel_target[len("/srv/spreadsheets/"):]
    elif rel_target.startswith("/"):
        rel_target = ""

    if not rel_target:
        folder = item.get("folder", "root")
        fname = ensure_office_extension(item.get("name", ""), item.get("category", ""))
        rel_target = f"{folder}/{fname}" if (folder and folder != "root") else fname

    return f"{COLLABORA_WEB_URL}/edit/{urllib.parse.quote(rel_target, safe='')}"


# ----------------------------------------------------------------------
# DIRECT GOOGLE DRIVE REST API v3 EXPORT, UPLOAD, & DELETE
# ----------------------------------------------------------------------

def export_file_via_api(item_id: str, category: str, local_dest_path: str) -> Tuple[bool, str]:
    """Export a Google-native document directly via Google Drive REST API v3 `/files/{id}/export`."""
    export_mime_map = {
        "spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "drawing": "image/svg+xml",
    }
    export_mime = export_mime_map.get(category, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    export_url = (
        f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(item_id)}/export?"
        + urllib.parse.urlencode({"mimeType": export_mime})
    )

    try:
        file_bytes = gdrive_api_request(export_url, raw_bytes=True)
    except RuntimeError as exc:
        if "EXPORT_SIZE_LIMIT_EXCEEDED" in str(exc):
            # Fallback to exportLinks for >10 MB Google Docs/Sheets
            meta_url = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(item_id)}?fields=exportLinks"
            meta = gdrive_api_request(meta_url)
            export_links = meta.get("exportLinks") or {}
            alt_url = export_links.get(export_mime)
            if not alt_url:
                return False, f"File exceeds 10 MB export limit and no exportLink found for {export_mime}"
            file_bytes = gdrive_api_request(alt_url, raw_bytes=True)
        else:
            return False, f"Google Drive API v3 export failed: {exc}"
    except Exception as exc:
        return False, f"Google Drive API v3 export failed: {exc}"

    os.makedirs(os.path.dirname(local_dest_path), exist_ok=True)
    with open(local_dest_path, "wb") as f:
        f.write(file_bytes)
    return True, f"Exported {len(file_bytes)} bytes via Google Drive API v3"


def upload_to_miniwopi(local_file_path: str, folder: str, filename: str) -> Tuple[bool, str]:
    """Upload an exported Office file to Collabora's miniwopi host via HTTP POST /upload."""
    boundary = f"----GDriveTriageBoundary{uuid.uuid4().hex}"
    with open(local_file_path, "rb") as f:
        file_bytes = f.read()

    parts: List[bytes] = []
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(b'Content-Disposition: form-data; name="folder"\r\n\r\n')
    parts.append(f"{folder}\r\n".encode("utf-8"))

    safe_fname = filename.replace('"', "")
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{safe_fname}"\r\n'.encode("utf-8")
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    upload_url = f"{COLLABORA_WEB_URL}/upload"

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def http_error_303(self, req, fp, code, msg, headers):
            return fp

        def http_error_302(self, req, fp, code, msg, headers):
            return fp

    opener = urllib.request.build_opener(NoRedirectHandler)
    req = urllib.request.Request(
        upload_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        resp = opener.open(req, timeout=30)
        code = getattr(resp, "status", resp.getcode())
        if code in (200, 201, 302, 303):
            return True, "Uploaded to Collabora via miniwopi HTTP endpoint"
        return False, f"Unexpected HTTP status {code} from {upload_url}"
    except urllib.error.HTTPError as exc:
        if exc.code in (302, 303):
            return True, "Uploaded to Collabora via miniwopi HTTP endpoint"
        return False, f"HTTP upload to {upload_url} failed: HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"HTTP upload to {upload_url} failed: {exc}"


def import_item_to_collabora(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Convert Google-native document via Google Drive REST API v3 and upload to Collabora."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    folder = item["folder"]
    category = item["category"]
    name = ensure_office_extension(item["name"], category)

    local_tmp_dir = os.path.expanduser("~/.cache/gdrive_triage/export")
    os.makedirs(local_tmp_dir, exist_ok=True)
    local_tmp_file = os.path.join(local_tmp_dir, name)

    print(f"Exporting Google Doc ID '{item_id}' ('{name}') via Google Drive API v3...")
    ok_exp, exp_msg = export_file_via_api(item_id, category, local_tmp_file)
    if not ok_exp:
        return False, exp_msg

    target_subfolder = folder if (folder and folder != "root") else ""
    target_rel_path = f"{target_subfolder}/{name}" if target_subfolder else name

    ok, upload_msg = upload_to_miniwopi(local_tmp_file, target_subfolder, name)
    if os.path.exists(local_tmp_file):
        os.remove(local_tmp_file)

    if not ok:
        return False, upload_msg

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items
            SET triage_status = 'imported_collabora',
                target_path = ?,
                updated_at = ?
            WHERE id = ?
        """, (target_rel_path, now_iso, item_id))

    return True, f"Converted via Drive API v3 & imported to Collabora ({COLLABORA_WEB_URL}): {target_rel_path}"


def archive_item_to_nas(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Convert Google-native file via Google Drive REST API v3 and save to Synology NAS archive directory."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    folder = item["folder"]
    category = item["category"]
    name = ensure_office_extension(item["name"], category)

    rel_dir = folder if (folder and folder != "root") else ""
    target_dir = os.path.join(NAS_ARCHIVE_DIR, rel_dir) if rel_dir else NAS_ARCHIVE_DIR
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, name)

    ok_exp, exp_msg = export_file_via_api(item_id, category, target_path)
    if not ok_exp:
        return False, exp_msg

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items
            SET triage_status = 'archived_nas',
                target_path = ?,
                updated_at = ?
            WHERE id = ?
        """, (target_path, now_iso, item_id))

    return True, f"Converted via Drive API v3 & archived to NAS: {target_path}"


def delete_item_from_gdrive(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str, bool]:
    """
    Move the Google-native document to Google Drive Trash directly by File ID via
    Google Drive REST API v3 (`PATCH /drive/v3/files/{fileId}` with `{"trashed": true}`).
    Returns `(ok, message, needs_oauth_upgrade)`.
    """
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found", False

    rel_path = item["path"]
    cur_status = item["triage_status"]

    print(f"Moving '{rel_path}' (ID: {item_id}) to Google Drive Trash via Drive API v3...")
    url = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(item_id)}?fields=id,name,trashed"
    try:
        gdrive_api_request(url, method="PATCH", json_body={"trashed": True})
    except RuntimeError as exc:
        err_str = str(exc)
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in err_str or "drive.readonly" in err_str:
            return False, err_str, True
        if "HTTP 404" in err_str:
            # Already deleted from Google Drive
            pass
        else:
            return False, err_str, False
    except Exception as exc:
        return False, f"Google Drive API v3 trash failed: {exc}", False

    new_status = cur_status if cur_status in ("imported_collabora", "archived_nas") else "deleted"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items
            SET triage_status = ?,
                drive_deleted = 1,
                updated_at = ?
            WHERE id = ?
        """, (new_status, now_iso, item_id))

    return True, f"Moved '{rel_path}' to Google Drive Trash via Drive API v3.", False


def update_item_status(item_id: str, status: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    conn = get_db(db_path)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("UPDATE items SET triage_status = ?, updated_at = ? WHERE id = ?", (status, now_iso, item_id))
    return True, f"Status updated to {status}"


# ----------------------------------------------------------------------
# WEB SERVER & UI
# ----------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Google Docs &amp; Sheets Migration to Collabora (Drive API v3)</title>
  <style>
    :root {
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --success: #16a34a;
      --warning: #d97706;
      --purple: #9333ea;
      --danger: #dc2626;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 0;
    }
    header {
      background: #1e293b;
      color: white;
      padding: 0.9rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.75rem;
      box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    header h1 {
      margin: 0;
      font-size: 1.2rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .header-links {
      font-size: 0.88rem;
      display: flex;
      align-items: center;
      gap: 0.85rem;
      flex-wrap: wrap;
    }
    .header-links a {
      color: #93c5fd;
      text-decoration: none;
      font-weight: 500;
    }
    .header-links a:hover {
      text-decoration: underline;
    }
    .oauth-pill {
      padding: 0.3rem 0.65rem;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }
    .oauth-pill.readonly {
      background: #fef3c7;
      color: #92400e;
      border-color: #f59e0b;
    }
    .oauth-pill.full {
      background: #dcfce7;
      color: #166534;
      border-color: #22c55e;
    }
    .container {
      max-width: 1440px;
      margin: 1.25rem auto;
      padding: 0 1.5rem;
    }
    .info-banner {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1e3a8a;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-size: 0.86rem;
      line-height: 1.45;
    }
    .oauth-banner {
      background: #fffbeb;
      border: 1px solid #fcd34d;
      color: #78350f;
      padding: 0.85rem 1rem;
      border-radius: 8px;
      margin-bottom: 1.1rem;
      font-size: 0.88rem;
      display: none;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-bottom: 1.25rem;
    }
    .stat-card {
      background: var(--card-bg);
      padding: 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .stat-card .label {
      font-size: 0.76rem;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 600;
    }
    .stat-card .val {
      font-size: 1.55rem;
      font-weight: bold;
      margin-top: 0.25rem;
    }
    .toolbar {
      background: var(--card-bg);
      padding: 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      margin-bottom: 1.25rem;
    }
    select, input, button {
      padding: 0.5rem 0.75rem;
      font-size: 0.88rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: white;
    }
    select#folder-select {
      max-width: 390px;
    }
    input[type="search"] {
      flex: 1;
      min-width: 220px;
    }
    button.btn-primary {
      background: var(--primary);
      color: white;
      border: none;
      cursor: pointer;
      font-weight: 500;
    }
    button.btn-primary:hover {
      background: var(--primary-hover);
    }
    button.btn-warning {
      background: #d97706;
      color: white;
      border: none;
      cursor: pointer;
      font-weight: 600;
    }
    button.btn-warning:hover {
      background: #b45309;
    }
    .filter-pills {
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
    }
    .pill {
      font-size: 0.8rem;
      padding: 0.3rem 0.65rem;
      border-radius: 20px;
      border: 1px solid var(--border);
      background: #f1f5f9;
      cursor: pointer;
      user-select: none;
    }
    .pill.active {
      background: var(--primary);
      color: white;
      border-color: var(--primary);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--card-bg);
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    th, td {
      padding: 0.75rem 1rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
      font-size: 0.88rem;
    }
    th {
      background: #f8fafc;
      font-weight: 600;
      color: var(--text-muted);
    }
    tr:hover {
      background: #f8fafc;
    }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.5rem;
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 4px;
    }
    .badge-pending { background: #fef3c7; color: #92400e; }
    .badge-imported_collabora { background: #dcfce7; color: #166534; }
    .badge-archived_nas { background: #e0e7ff; color: #3730a3; }
    .badge-deleted { background: #fee2e2; color: #991b1b; }
    .badge-cat {
      background: #f1f5f9;
      color: #334155;
      font-size: 0.75rem;
      border: 1px solid #cbd5e1;
    }
    .btn-group {
      display: flex;
      gap: 0.35rem;
      flex-wrap: wrap;
      align-items: center;
    }
    .btn-action {
      padding: 0.28rem 0.55rem;
      font-size: 0.76rem;
      border-radius: 4px;
      border: 1px solid var(--border);
      cursor: pointer;
      background: white;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.2rem;
    }
    .btn-action:hover {
      background: #f1f5f9;
    }
    .btn-action.collab {
      color: var(--primary);
      border-color: #93c5fd;
    }
    .btn-action.collab:hover {
      background: #eff6ff;
    }
    .btn-action.nas {
      color: var(--purple);
      border-color: #d8b4fe;
    }
    .btn-action.nas:hover {
      background: #faf5ff;
    }
    .btn-action.delete {
      color: var(--danger);
      border-color: #fca5a5;
    }
    .btn-action.delete:hover {
      background: #fef2f2;
    }
    .file-name {
      font-weight: 500;
      word-break: break-all;
      color: #1e40af;
      text-decoration: none;
    }
    .file-name:hover {
      text-decoration: underline;
    }
    .file-path {
      font-size: 0.75rem;
      color: var(--text-muted);
      display: block;
      margin-top: 0.15rem;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.55);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 2000;
      padding: 1rem;
    }
    .modal {
      background: white;
      max-width: 620px;
      width: 100%;
      border-radius: 10px;
      padding: 1.5rem;
      box-shadow: 0 10px 25px rgba(0,0,0,0.2);
    }
    .modal h2 {
      margin-top: 0;
      font-size: 1.15rem;
    }
    .modal ol {
      padding-left: 1.25rem;
      line-height: 1.55;
      font-size: 0.9rem;
    }
    .modal li {
      margin-bottom: 0.75rem;
    }
    .modal input[type="text"] {
      width: 100%;
      box-sizing: border-box;
      margin-top: 0.4rem;
      font-family: monospace;
      font-size: 0.82rem;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 0.5rem;
      margin-top: 1rem;
    }
    #toast {
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      padding: 0.75rem 1.25rem;
      background: #1e293b;
      color: white;
      border-radius: 6px;
      box-shadow: 0 4px 6px rgba(0,0,0,0.15);
      display: none;
      z-index: 3000;
      font-size: 0.9rem;
      max-width: 480px;
    }
  </style>
</head>
<body>
  <header>
    <h1><span>🗂️</span> Google Docs &amp; Sheets Migration to Collabora <span style="font-size:0.75rem;background:#334155;padding:0.18rem 0.5rem;border-radius:4px;font-weight:500;">Direct Drive API v3</span></h1>
    <div class="header-links">
      <span id="oauth-badge" class="oauth-pill readonly" onclick="openOAuthModal()" title="Click to authorize full Google Drive delete permission">🔒 Checking OAuth Scope...</span>
      <a href="__COLLABORA_WEB_URL__/" id="link-collabora" target="_blank">Collabora Editor ↗</a>
      <a href="https://drive.google.com/" target="_blank">Google Drive Web ↗</a>
    </div>
  </header>

  <div class="container">
    <div class="info-banner">
      <strong>Direct Google Drive REST API v3 · Only Owned Google-Native Documents Shown:</strong>
      Queries <code>drive.googleapis.com/drive/v3/files</code> directly with <code>'me' in owners</code> across all folder depths.
      All <strong>"Shared with me" files owned by other people are hidden</strong>, and regular PDFs/binary files are excluded.
      Conversion to <code>.xlsx</code> / <code>.docx</code> / <code>.pptx</code> happens on-demand via the Drive API <code>/export</code> endpoint when you click <strong>✨ To Collabora</strong> or <strong>💾 To NAS</strong>.
    </div>

    <div id="oauth-warning-banner" class="oauth-banner">
      <div>
        <strong>🔑 Why Delete Failed on "Copy of First Week Launch Checklist - 12DoS":</strong>
        The current Google OAuth token was originally created for read-only sync (<code>drive.readonly</code> scope).
        Reading and converting to Collabora work, but moving files to Google Drive Trash via the Drive API requires full <code>drive</code> scope.
      </div>
      <button class="btn-warning" onclick="openOAuthModal()">🔑 Authorize Delete Permission (2 Clicks)</button>
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="label">Owned Google-Native Docs</div>
        <div class="val" id="stat-total">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Pending Triage</div>
        <div class="val" id="stat-pending" style="color:var(--warning)">-</div>
      </div>
      <div class="stat-card">
        <div class="label">In Collabora</div>
        <div class="val" id="stat-imported" style="color:var(--success)">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Archived to NAS</div>
        <div class="val" id="stat-archived" style="color:var(--purple)">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Deleted from Drive</div>
        <div class="val" id="stat-deleted" style="color:var(--danger)">-</div>
      </div>
    </div>

    <div class="toolbar">
      <label for="folder-select">Folder / Subfolder:</label>
      <select id="folder-select" onchange="loadItems()">
        <option value="all">All Folders</option>
      </select>
      <button class="btn-primary" onclick="rescanAll()">🔄 Sync All from Drive API</button>

      <input type="search" id="search-box" placeholder="Instant search by document title or folder path..." oninput="debounceSearch()">

      <div class="filter-pills" id="category-pills">
        <div class="pill active" data-cat="all" onclick="setCategory('all')">All Types</div>
        <div class="pill" data-cat="spreadsheet" onclick="setCategory('spreadsheet')">📊 Sheets</div>
        <div class="pill" data-cat="document" onclick="setCategory('document')">📝 Docs</div>
        <div class="pill" data-cat="presentation" onclick="setCategory('presentation')">📽️ Slides</div>
        <div class="pill" data-cat="drawing" onclick="setCategory('drawing')">🎨 Drawings</div>
      </div>

      <div class="filter-pills" id="status-pills">
        <div class="pill active" data-status="all" onclick="setStatus('all')">All Statuses</div>
        <div class="pill" data-status="pending" onclick="setStatus('pending')">Pending</div>
        <div class="pill" data-status="imported_collabora" onclick="setStatus('imported_collabora')">In Collabora</div>
        <div class="pill" data-status="archived_nas" onclick="setStatus('archived_nas')">On NAS</div>
        <div class="pill" data-status="deleted" onclick="setStatus('deleted')">Deleted from Drive</div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th>Google Document &amp; Full Folder Path</th>
          <th>Google Type &amp; Export Target</th>
          <th>Conversion State</th>
          <th>Modified</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="items-body">
        <tr><td colspan="6" style="text-align:center;padding:2rem;">Loading owned Google-native documents...</td></tr>
      </tbody>
    </table>
  </div>

  <!-- 2-Click OAuth Write-Scope Authorization Modal -->
  <div id="oauth-modal" class="modal-backdrop">
    <div class="modal">
      <h2>🔑 Authorize Google Drive Delete Permission (Full <code>drive</code> Scope)</h2>
      <p style="font-size:0.88rem;color:var(--text-muted);margin-top:0;">
        Your current token has <code>drive.readonly</code> scope (which allows listing and exporting documents to Collabora, but blocks trashing/deleting files). Complete these 2 quick steps to grant write/trash permission:
      </p>
      <ol>
        <li>
          Click <a href="/api/oauth/start" target="_blank" class="btn-primary" style="text-decoration:none;padding:0.35rem 0.7rem;border-radius:5px;display:inline-block;">1. Open Google Permission Page ↗</a>
          and click <strong>Allow</strong> on Google's consent screen.
        </li>
        <li>
          After clicking Allow, your browser tab will redirect to a <code>http://127.0.0.1:53682/?state=...&amp;code=...</code> address (which will say <em>"Unable to connect"</em> because port 53682 is on Debmox).<br>
          <strong>Copy that entire <code>http://127.0.0.1:53682/...</code> URL from your browser's address bar and paste it below:</strong>
          <input type="text" id="oauth-callback-input" placeholder="http://127.0.0.1:53682/?state=...&code=4/0A...">
        </li>
      </ol>
      <div id="oauth-modal-status" style="font-size:0.85rem;margin-top:0.5rem;display:none;"></div>
      <div class="modal-actions">
        <button onclick="closeOAuthModal()">Cancel</button>
        <button class="btn-primary" onclick="completeOAuthUpgrade()">✅ 2. Complete Authorization</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    let currentCategory = 'all';
    let currentStatus = 'all';
    let searchTimeout = null;
    let pendingDeleteItemId = null;

    async function fetchOAuthStatus() {
      try {
        const res = await fetch('/api/oauth/status');
        const data = await res.json();
        const badge = document.getElementById('oauth-badge');
        const banner = document.getElementById('oauth-warning-banner');
        if (data.can_delete) {
          badge.className = 'oauth-pill full';
          badge.innerText = '✅ OAuth: Full Drive API (Read/Export/Delete)';
          banner.style.display = 'none';
        } else {
          badge.className = 'oauth-pill readonly';
          badge.innerText = '🔒 OAuth: Read-Only (Click to Enable Delete)';
          banner.style.display = 'flex';
        }
      } catch (e) {}
    }

    async function fetchStats() {
      const res = await fetch('/api/stats');
      const data = await res.json();
      document.getElementById('stat-total').innerText = data.total;
      document.getElementById('stat-pending').innerText = data.pending;
      document.getElementById('stat-imported').innerText = data.imported;
      document.getElementById('stat-archived').innerText = data.archived;
      document.getElementById('stat-deleted').innerText = data.deleted;

      const sel = document.getElementById('folder-select');
      const cur = sel.value;
      const topFolders = data.folders.top_folders || [];
      const subFolders = data.folders.subfolders || [];

      sel.innerHTML = `<option value="all">All Folders (${data.total} docs across ${topFolders.length} top folders / ${subFolders.length} paths)</option>`;

      for (const tf of topFolders) {
        const topName = tf.top_folder;
        const subs = subFolders.filter(sf => sf.top_folder === topName);
        if (subs.length > 1 || (subs.length === 1 && subs[0].folder !== topName)) {
          const optTop = document.createElement('option');
          optTop.value = 'top:' + topName;
          optTop.innerText = `📁 ${topName} — All ${subs.length} Subfolders (${tf.total} docs, ${tf.pending} pending)`;
          if (optTop.value === cur) optTop.selected = true;
          sel.appendChild(optTop);

          for (const sf of subs) {
            const optSub = document.createElement('option');
            optSub.value = sf.folder;
            optSub.innerText = `   ↳ ${sf.folder} (${sf.total} docs, ${sf.pending} pending)`;
            if (optSub.value === cur) optSub.selected = true;
            sel.appendChild(optSub);
          }
        } else {
          const opt = document.createElement('option');
          opt.value = topName;
          opt.innerText = `📁 ${topName} (${tf.total} docs, ${tf.pending} pending)`;
          if (opt.value === cur) opt.selected = true;
          sel.appendChild(opt);
        }
      }
    }

    function setCategory(cat) {
      currentCategory = cat;
      document.querySelectorAll('#category-pills .pill').forEach(p => {
        p.classList.toggle('active', p.dataset.cat === cat);
      });
      loadItems();
    }

    function setStatus(st) {
      currentStatus = st;
      document.querySelectorAll('#status-pills .pill').forEach(p => {
        p.classList.toggle('active', p.dataset.status === st);
      });
      loadItems();
    }

    function debounceSearch() {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(loadItems, 180);
    }

    function formatTypeBadge(cat) {
      if (cat === 'spreadsheet') return '<span class="badge badge-cat">📊 Google Sheet → .xlsx</span>';
      if (cat === 'document') return '<span class="badge badge-cat">📝 Google Doc → .docx</span>';
      if (cat === 'presentation') return '<span class="badge badge-cat">📽️ Google Slides → .pptx</span>';
      if (cat === 'drawing') return '<span class="badge badge-cat">🎨 Google Drawing → .svg</span>';
      return '<span class="badge badge-cat">' + escapeHtml(cat) + '</span>';
    }

    function formatConversionState(it) {
      if (it.triage_status === 'imported_collabora') {
        return '<span style="color:var(--success);font-weight:600;font-size:0.82rem">Converted &amp; in Collabora</span>';
      }
      if (it.triage_status === 'archived_nas') {
        return '<span style="color:var(--purple);font-weight:600;font-size:0.82rem">Converted &amp; on NAS</span>';
      }
      if (it.triage_status === 'deleted' || it.drive_deleted === 1) {
        return '<span style="color:var(--danger);font-size:0.82rem">Moved to Google Drive Trash</span>';
      }
      return '<span style="color:#64748b;font-size:0.82rem">Not converted yet (converts on click)</span>';
    }

    function formatStatusBadges(it) {
      let badges = '';
      if (it.triage_status === 'imported_collabora') {
        badges += '<span class="badge badge-imported_collabora">In Collabora</span> ';
      } else if (it.triage_status === 'archived_nas') {
        badges += '<span class="badge badge-archived_nas">Archived NAS</span> ';
      } else if (it.triage_status === 'deleted') {
        badges += '<span class="badge badge-deleted">Deleted</span> ';
      } else {
        badges += '<span class="badge badge-pending">Pending</span> ';
      }
      if (it.drive_deleted === 1 && it.triage_status !== 'deleted') {
        badges += '<span class="badge badge-deleted">Deleted from Drive</span>';
      }
      return badges;
    }

    function formatDate(dt) {
      if (!dt) return '-';
      return dt.substring(0, 10);
    }

    function stripPreviewExtension(name) {
      return name.replace(/\.(xlsx|docx|pptx|svg)$/i, '');
    }

    async function loadItems() {
      const folder = document.getElementById('folder-select').value;
      const q = document.getElementById('search-box').value;
      const params = new URLSearchParams({
        folder: folder,
        category: currentCategory,
        status: currentStatus,
        q: q
      });
      const res = await fetch('/api/items?' + params.toString());
      const items = await res.json();
      const tbody = document.getElementById('items-body');

      if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#64748b">No matching user-owned Google-native documents found.</td></tr>';
        return;
      }

      let htmlRows = '';
      for (const it of items) {
        const driveUrl = 'https://drive.google.com/open?id=' + encodeURIComponent(it.id);
        const cleanTitle = stripPreviewExtension(it.name);
        const editUrl = it.collabora_edit_url;
        const safeNameJs = escapeHtml(cleanTitle).replace(/'/g, "\\'");

        let actionBtns = '';
        if (it.triage_status === 'imported_collabora') {
          actionBtns += `<a href="${editUrl}" target="_blank" class="btn-action collab" title="Inspect converted file in Collabora">Open in Collabora ↗</a>`;
          if (!it.drive_deleted) {
            actionBtns += `<button class="btn-action delete" title="Move original to Google Drive Trash via Drive API v3" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete from Drive</button>`;
          }
        } else if (it.triage_status === 'archived_nas') {
          actionBtns += `<button class="btn-action collab" title="Also convert &amp; import to Collabora" onclick="doAction('${it.id}', 'collabora')">✨ To Collabora</button>`;
          if (!it.drive_deleted) {
            actionBtns += `<button class="btn-action delete" title="Move original to Google Drive Trash via Drive API v3" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete from Drive</button>`;
          }
        } else if (it.triage_status === 'deleted' || it.drive_deleted === 1) {
          actionBtns = `<span style="color:var(--text-muted);font-size:0.8rem">Deleted from Google Drive</span>`;
        } else {
          actionBtns = `
            <button class="btn-action collab" title="Export via Drive API v3 &amp; upload to Collabora" onclick="doAction('${it.id}', 'collabora')">✨ To Collabora</button>
            <button class="btn-action nas" title="Export via Drive API v3 &amp; archive to NAS" onclick="doAction('${it.id}', 'nas')">💾 To NAS</button>
            <button class="btn-action delete" title="Move directly to Google Drive Trash via Drive API v3" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete</button>
          `;
        }

        htmlRows += `
          <tr>
            <td>
              <a href="${driveUrl}" target="_blank" class="file-name" title="Open original in Google Drive">${escapeHtml(cleanTitle)}</a>
              <span class="file-path">Folder: <strong>${escapeHtml(it.folder)}</strong> · Export path: ${escapeHtml(it.path)}</span>
            </td>
            <td>${formatTypeBadge(it.category)}</td>
            <td>${formatConversionState(it)}</td>
            <td>${formatDate(it.mod_time)}</td>
            <td>${formatStatusBadges(it)}</td>
            <td><div class="btn-group">${actionBtns}</div></td>
          </tr>
        `;
      }
      tbody.innerHTML = htmlRows;
    }

    function confirmDelete(id, title) {
      if (!confirm('Move "' + title + '" to Google Drive Trash via Google Drive API v3?')) {
        return;
      }
      doAction(id, 'delete');
    }

    async function doAction(id, act) {
      showToast('Processing via Google Drive API v3: ' + act + '...');
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, action: act })
      });
      const data = await res.json();
      if (data.ok) {
        showToast(data.message);
        loadItems();
        fetchStats();
      } else {
        showToast('Error: ' + data.message, true);
        if (data.needs_oauth_upgrade) {
          pendingDeleteItemId = id;
          openOAuthModal();
        }
      }
    }

    async function rescanAll() {
      showToast('Querying Google Drive REST API v3 for all owned Google-native documents...');
      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: 'all' })
      });
      const data = await res.json();
      showToast(data.message, !data.ok);
      fetchStats();
      loadItems();
    }

    function openOAuthModal() {
      document.getElementById('oauth-modal').style.display = 'flex';
      document.getElementById('oauth-modal-status').style.display = 'none';
      document.getElementById('oauth-callback-input').focus();
    }

    function closeOAuthModal() {
      document.getElementById('oauth-modal').style.display = 'none';
    }

    async function completeOAuthUpgrade() {
      const val = document.getElementById('oauth-callback-input').value.trim();
      const st = document.getElementById('oauth-modal-status');
      st.style.display = 'block';
      st.style.color = '#1e3a8a';
      st.innerText = 'Exchanging authorization code with Google OAuth 2.0...';

      const res = await fetch('/api/oauth/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_url: val })
      });
      const data = await res.json();
      if (data.ok) {
        st.style.color = 'var(--success)';
        st.innerText = '✅ ' + data.message;
        document.getElementById('oauth-callback-input').value = '';
        await fetchOAuthStatus();
        setTimeout(() => {
          closeOAuthModal();
          if (pendingDeleteItemId) {
            const retryId = pendingDeleteItemId;
            pendingDeleteItemId = null;
            doAction(retryId, 'delete');
          }
        }, 900);
      } else {
        st.style.color = 'var(--danger)';
        st.innerText = '❌ ' + data.message;
      }
    }

    function showToast(msg, isError = false) {
      const toast = document.getElementById('toast');
      toast.innerText = msg;
      toast.style.background = isError ? 'var(--danger)' : '#1e293b';
      toast.style.display = 'block';
      setTimeout(() => { toast.style.display = 'none'; }, 5500);
    }

    function escapeHtml(s) {
      if (!s) return '';
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Init
    fetchOAuthStatus();
    fetchStats();
    loadItems();
  </script>
</body>
</html>
"""


class TriageHandler(BaseHTTPRequestHandler):
    db_path: str = DEFAULT_DB_PATH

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def send_json(self, data: Any, code: int = 200):
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_html(self, content: str, code: int = 200):
        body = content.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path == "/" or path == "/index.html":
            rendered = HTML_TEMPLATE.replace("__COLLABORA_WEB_URL__", html.escape(COLLABORA_WEB_URL))
            return self.send_html(rendered)

        if path == "/api/oauth/status":
            return self.send_json(get_oauth_scope_status(force_check=False))

        if path == "/api/oauth/start":
            ok, target = start_oauth_upgrade_session()
            if not ok:
                return self.send_html(f"<h3>Failed to start OAuth session</h3><p>{html.escape(target)}</p>", 500)
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return

        if path == "/api/stats":
            conn = get_db(self.db_path)
            total = conn.execute(
                "SELECT COUNT(*) FROM items WHERE is_dir = 0 AND size = -1 AND owned_by_me = 1"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'pending' AND drive_deleted = 0 AND is_dir = 0 AND size = -1 AND owned_by_me = 1"
            ).fetchone()[0]
            imported = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'imported_collabora' AND is_dir = 0 AND size = -1 AND owned_by_me = 1"
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'archived_nas' AND is_dir = 0 AND size = -1 AND owned_by_me = 1"
            ).fetchone()[0]
            deleted = conn.execute(
                "SELECT COUNT(*) FROM items WHERE (triage_status = 'deleted' OR drive_deleted = 1) AND is_dir = 0 AND size = -1 AND owned_by_me = 1"
            ).fetchone()[0]
            folders = get_folders_summary(self.db_path)
            return self.send_json({
                "total": total,
                "pending": pending,
                "imported": imported,
                "archived": archived,
                "deleted": deleted,
                "folders": folders,
            })

        if path == "/api/items":
            qs = urllib.parse.parse_qs(url.query)
            folder = qs.get("folder", ["all"])[0]
            category = qs.get("category", ["all"])[0]
            status = qs.get("status", ["all"])[0]
            query = qs.get("q", [""])[0]
            items = get_items(self.db_path, folder=folder, category=category, status=status, query=query)
            return self.send_json(items)

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if path == "/api/oauth/complete":
            cb_url = payload.get("callback_url", "")
            ok, msg = complete_oauth_upgrade_session(cb_url)
            return self.send_json({"ok": ok, "message": msg})

        if path == "/api/action":
            item_id = payload.get("id")
            action = payload.get("action")
            if not item_id or not action:
                return self.send_json({"ok": False, "message": "Missing id or action"}, 400)

            needs_upgrade = False
            try:
                if action == "collabora":
                    ok, msg = import_item_to_collabora(item_id, self.db_path)
                elif action == "nas":
                    ok, msg = archive_item_to_nas(item_id, self.db_path)
                elif action == "delete":
                    ok, msg, needs_upgrade = delete_item_from_gdrive(item_id, self.db_path)
                elif action == "pending":
                    ok, msg = update_item_status(item_id, "pending", self.db_path)
                else:
                    ok, msg = False, f"Unknown action: {action}"
            except Exception as exc:
                ok, msg = False, str(exc)

            return self.send_json({
                "ok": ok,
                "message": msg,
                "needs_oauth_upgrade": needs_upgrade,
            })

        if path == "/api/scan":
            try:
                count = index_all_via_api(db_path=self.db_path)
                return self.send_json({
                    "ok": True,
                    "message": f"Synced {count} user-owned Google-native documents across all folders via Google Drive API v3.",
                })
            except Exception as exc:
                return self.send_json({"ok": False, "message": f"Drive API v3 sync failed: {exc}"})

        self.send_response(404)
        self.end_headers()


def serve(port: int = DEFAULT_PORT, db_path: str = DEFAULT_DB_PATH):
    get_db(db_path)
    TriageHandler.db_path = db_path
    server = ThreadingHTTPServer(("0.0.0.0", port), TriageHandler)
    print(f"Google Drive Triage UI (Direct Drive API v3) serving on http://0.0.0.0:{port} (Collabora: {COLLABORA_WEB_URL}, DB: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Google Drive Interactive Migration & Triage Assistant (Direct Drive API v3)")
    subparsers = parser.add_subparsers(dest="command")

    # index / index-all
    index_parser = subparsers.add_parser("index", help="Sync all user-owned Google-native docs from Google Drive API v3 into SQLite")
    index_parser.add_argument("folder", nargs="?", default="all", help="Optional folder name (syncs full Drive via API v3)")
    index_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    index_parser.add_argument("--seed-docs", default=None, help="Optional path to pre-fetched owned docs JSON")

    index_all_parser = subparsers.add_parser("index-all", help="Sync all user-owned Google-native docs from Google Drive API v3 into SQLite")
    index_all_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    index_all_parser.add_argument("--seed-docs", default=None, help="Optional path to pre-fetched owned docs JSON")

    # stats
    stats_parser = subparsers.add_parser("stats", help="Show database summary statistics")
    stats_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Run the Web UI server")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on (default: 8085)")
    serve_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    args = parser.parse_args()

    if args.command in ("index", "index-all"):
        index_all_via_api(db_path=args.db, seed_docs_path=getattr(args, "seed_docs", None))
    elif args.command == "stats":
        summary = get_folders_summary(args.db)
        print(f"User-Owned Google-Native Documents summary in database ({args.db}):")
        print("\nTop-Level Folders:")
        for f in summary["top_folders"]:
            print(
                f"  {f['top_folder']:<28} Subfolders: {f['subfolder_count']:<3} Total: {f['total']:<4} "
                f"Pending: {f['pending']:<4} In Collabora: {f['imported']:<3} On NAS: {f['archived']:<3} Deleted: {f['deleted']}"
            )
        print("\nFull Subfolder Paths:")
        for f in summary["subfolders"]:
            print(
                f"  {f['folder']:<55} Total: {f['total']:<4} Pending: {f['pending']:<4} "
                f"In Collabora: {f['imported']:<3} On NAS: {f['archived']:<3} Deleted: {f['deleted']}"
            )
    elif args.command == "serve":
        serve(port=args.port, db_path=args.db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
