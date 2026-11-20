#!/usr/bin/env python3
"""
gdrive_triage.py - Interactive Google Drive Migration & Triage Assistant

Features:
- Caches ONLY Google-native documents (Google Sheets, Google Docs, Google Slides,
  Google Drawings where Size == -1 in rclone lsjson) in a local SQLite database
  to prevent API rate limits. Regular binary files (PDFs, native uploaded Excel/Word
  files, images, archives, CAD) that can be opened without Google Drive tools are
  excluded.
- Note on file conversion: Google-native documents are NOT converted during indexing
  (rclone merely previews their target export extension .xlsx/.docx/.pptx). Actual
  conversion occurs on-demand only when the user clicks "To Collabora" or "To NAS".
- Web UI (vanilla HTML/JS/CSS, zero dependencies):
  * Filter by folder, Google Doc type, and triage status.
  * Instant search across cached files (< 5ms).
  * 1-Click "Convert & Import to Collabora Online" (via miniwopi HTTP POST /upload).
  * 1-Click "Convert & Archive to NAS".
  * 1-Click "Delete from Google Drive" (available directly on pending items and
    after inspecting a converted file in Collabora or on NAS; no Ignore button).
  * Direct Google Drive link and direct Collabora Editor link (/edit/<file>) for
    every file.
"""

import argparse
import datetime
import html
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/gdrive_triage/gdrive_triage.db")
DEFAULT_PORT = 8085
COLLABORA_WEB_URL = os.environ.get("COLLABORA_URL", "http://collabora").rstrip("/")
NAS_ARCHIVE_DIR = os.environ.get("NAS_ARCHIVE_DIR", os.path.expanduser("~/archive/google_drive"))
DEFAULT_EXCLUDES = [
    "backup",
    "Postbox",
    "temp_download_copy_needs_cleanup",
    ".ssh",
    "Colab Notebooks",
]


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
                is_dir INTEGER NOT NULL DEFAULT 0,
                mime_type TEXT,
                size INTEGER DEFAULT -1,
                mod_time TEXT,
                category TEXT,
                triage_status TEXT DEFAULT 'pending',
                target_path TEXT,
                notes TEXT,
                drive_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "drive_deleted" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN drive_deleted INTEGER NOT NULL DEFAULT 0")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_folder ON items(folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON items(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON items(triage_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON items(name)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                name TEXT PRIMARY KEY,
                last_scanned TIMESTAMP,
                item_count INTEGER DEFAULT 0
            )
        """)

        # Keep strictly Google-native documents (size = -1, is_dir = 0)
        conn.execute("DELETE FROM items WHERE size != -1 OR is_dir != 0")

        # Fix any legacy miscategorized Google Slides presentations
        conn.execute("""
            UPDATE items
            SET category = 'presentation'
            WHERE (mime_type LIKE '%presentation%' OR LOWER(name) LIKE '%.pptx' OR LOWER(name) LIKE '%.odp')
              AND category != 'presentation'
        """)
    return conn


def categorize_item(name: str, mime: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    lower_name = name.lower()
    lower_mime = mime.lower()

    if "spreadsheet" in lower_mime or lower_name.endswith((".xlsx", ".xls", ".ods", ".csv")):
        return "spreadsheet"
    if "presentation" in lower_mime or "powerpoint" in lower_mime or lower_name.endswith((".pptx", ".ppt", ".odp")):
        return "presentation"
    if (
        "wordprocessingml" in lower_mime
        or "msword" in lower_mime
        or "opendocument.text" in lower_mime
        or lower_name.endswith((".docx", ".doc", ".odt", ".rtf", ".txt"))
    ):
        return "document"
    if "svg" in lower_mime or lower_name.endswith(".svg"):
        return "drawing"
    return "other"


def index_folder(folder_name: str, db_path: str = DEFAULT_DB_PATH, max_depth: int = 4) -> int:
    """Scan a Google Drive folder via rclone lsjson and store ONLY Google-native documents (Size == -1) in SQLite."""
    is_root = not folder_name or folder_name == "root"
    folder_label = "root" if is_root else folder_name
    remote_path = "gdrive:" if is_root else f"gdrive:{folder_name}"
    effective_depth = 1 if is_root else max_depth

    print(f"Indexing Google-native documents in '{folder_label}' (max-depth {effective_depth})...")
    cmd = ["rclone", "--bind", "0.0.0.0", "lsjson", remote_path, "--files-only"]
    if is_root:
        cmd.append("--max-depth=1")
    else:
        cmd.extend([
            "--recursive",
            f"--max-depth={effective_depth}",
        ])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=45)
    except subprocess.TimeoutExpired:
        print(f"Timeout scanning folder '{folder_label}'.", file=sys.stderr)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Error running rclone on '{folder_label}': {exc.stderr}", file=sys.stderr)
        return 0

    items = json.loads(res.stdout)
    conn = get_db(db_path)
    count = 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with conn:
        for it in items:
            item_id = it.get("ID")
            if not item_id:
                continue
            is_dir = 1 if it.get("IsDir", False) else 0
            size = it.get("Size", -1)

            # Strictly include only Google-native virtual documents (Size == -1)
            if is_dir == 1 or size != -1:
                continue

            name = it.get("Name", "")
            rel_path = it.get("Path", "")
            full_path = rel_path if is_root else f"{folder_name}/{rel_path}"
            mime = it.get("MimeType", "")
            mod_time = it.get("ModTime", "")
            category = categorize_item(name, mime, False)

            existing = conn.execute(
                "SELECT path, folder, triage_status, target_path, notes, drive_deleted FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            triage_status = existing["triage_status"] if existing else "pending"
            target_path = existing["target_path"] if existing else None
            notes = existing["notes"] if existing else None
            drive_deleted = existing["drive_deleted"] if existing else 0
            if existing and len(existing["path"]) < len(full_path):
                full_path = existing["path"]
                eff_folder = existing["folder"]
            else:
                eff_folder = folder_label

            conn.execute("""
                INSERT INTO items (
                    id, path, name, folder, is_dir, mime_type, size, mod_time,
                    category, triage_status, target_path, notes, drive_deleted, updated_at
                )
                VALUES (?, ?, ?, ?, 0, ?, -1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    name = excluded.name,
                    folder = excluded.folder,
                    is_dir = 0,
                    mime_type = excluded.mime_type,
                    size = -1,
                    mod_time = excluded.mod_time,
                    category = excluded.category,
                    updated_at = excluded.updated_at
            """, (
                item_id, full_path, name, eff_folder, mime, mod_time,
                category, triage_status, target_path, notes, drive_deleted, now_iso
            ))
            count += 1

        conn.execute("""
            INSERT INTO folders (name, last_scanned, item_count)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_scanned = excluded.last_scanned,
                item_count = excluded.item_count
        """, (folder_label, now_iso, count))

    print(f"Indexed {count} Google-native document(s) in '{folder_label}'.")
    return count


def list_top_folders() -> List[str]:
    """List top-level directories on gdrive:"""
    cmd = ["rclone", "--bind", "0.0.0.0", "lsd", "gdrive:", "--max-depth=1"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    folders = []
    for line in res.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 5:
            # rclone lsd format: "-1 YYYY-MM-DD HH:MM:SS -1 <folder name>"
            folder_name = " ".join(parts[4:])
            folders.append(folder_name)
    return folders


def index_all_folders(
    exclude_folders: Optional[List[str]] = None,
    db_path: str = DEFAULT_DB_PATH,
    max_depth: int = 4,
):
    excludes = set(exclude_folders or DEFAULT_EXCLUDES)
    # Index root-level Google-native docs first
    index_folder("root", db_path=db_path, max_depth=1)

    all_folders = list_top_folders()
    print(f"Discovered {len(all_folders)} top-level folders on Google Drive.")
    for f in all_folders:
        if f in excludes:
            print(f"Skipping excluded folder: '{f}'")
            continue
        try:
            index_folder(f, db_path=db_path, max_depth=max_depth)
        except Exception as exc:
            print(f"Error indexing '{f}': {exc}", file=sys.stderr)


def get_folders_summary(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT folder,
               COUNT(*) as total,
               SUM(CASE WHEN triage_status = 'pending' AND drive_deleted = 0 THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN triage_status = 'imported_collabora' THEN 1 ELSE 0 END) as imported,
               SUM(CASE WHEN triage_status = 'archived_nas' THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN triage_status = 'deleted' OR drive_deleted = 1 THEN 1 ELSE 0 END) as deleted,
               SUM(CASE WHEN category IN ('spreadsheet', 'document', 'presentation') THEN 1 ELSE 0 END) as office_count
        FROM items
        WHERE is_dir = 0 AND size = -1
        GROUP BY folder
        HAVING total > 0
        ORDER BY pending DESC, total DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_items(
    db_path: str = DEFAULT_DB_PATH,
    folder: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    clauses = ["is_dir = 0", "size = -1"]
    params: List[Any] = []

    if folder and folder != "all":
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
        clauses.append("(name LIKE ? OR path LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

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
        rel_dir = os.path.dirname(item.get("path", "")) if item.get("folder") != "root" else ""
        fname = ensure_office_extension(item.get("name", ""), item.get("category", ""))
        rel_target = f"{rel_dir}/{fname}" if rel_dir else fname

    return f"{COLLABORA_WEB_URL}/edit/{urllib.parse.quote(rel_target, safe='')}"


def ensure_office_extension(name: str, category: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        if category == "spreadsheet":
            return name + ".xlsx"
        if category == "document":
            return name + ".docx"
        if category == "presentation":
            return name + ".pptx"
    return name


def upload_to_miniwopi(local_file_path: str, folder: str, filename: str) -> Tuple[bool, str]:
    """Upload an exported Office file to Collabora's miniwopi host via HTTP POST /upload."""
    boundary = f"----GDriveTriageBoundary{uuid.uuid4().hex}"
    with open(local_file_path, "rb") as f:
        file_bytes = f.read()

    parts: List[bytes] = []
    # folder field
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(b'Content-Disposition: form-data; name="folder"\r\n\r\n')
    parts.append(f"{folder}\r\n".encode("utf-8"))

    # file field
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
    """Convert Google-native document via rclone and upload to Collabora."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    rel_path = item["path"]
    folder = item["folder"]
    category = item["category"]
    name = ensure_office_extension(item["name"], category)

    local_tmp_dir = os.path.expanduser("~/.cache/gdrive_triage/export")
    os.makedirs(local_tmp_dir, exist_ok=True)
    local_tmp_file = os.path.join(local_tmp_dir, name)

    print(f"Converting & exporting 'gdrive:{rel_path}' (ID: {item_id}) to '{local_tmp_file}'...")
    copyid_cmd = ["rclone", "--bind", "0.0.0.0", "backend", "copyid", "gdrive:", item_id, local_tmp_file]
    res = subprocess.run(copyid_cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(local_tmp_file):
        # Fallback to copy by path
        copy_cmd = ["rclone", "--bind", "0.0.0.0", "copyto", f"gdrive:{rel_path}", local_tmp_file]
        res2 = subprocess.run(copy_cmd, capture_output=True, text=True)
        if res2.returncode != 0:
            return False, f"rclone export failed: {res.stderr or res2.stderr}"

    target_subfolder = os.path.dirname(rel_path) if folder != "root" else ""
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

    return True, f"Converted and imported to Collabora ({COLLABORA_WEB_URL}): {target_rel_path}"


def archive_item_to_nas(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Convert and copy Google-native file to Synology NAS archive directory."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    rel_path = item["path"]
    folder = item["folder"]
    name = ensure_office_extension(item["name"], item["category"])

    rel_dir = os.path.dirname(rel_path) if folder != "root" else ""
    target_dir = os.path.join(NAS_ARCHIVE_DIR, rel_dir) if rel_dir else NAS_ARCHIVE_DIR
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, name)

    copyid_cmd = ["rclone", "--bind", "0.0.0.0", "backend", "copyid", "gdrive:", item_id, target_path]
    res = subprocess.run(copyid_cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(target_path):
        copy_cmd = ["rclone", "--bind", "0.0.0.0", "copyto", f"gdrive:{rel_path}", target_path]
        res2 = subprocess.run(copy_cmd, capture_output=True, text=True)
        if res2.returncode != 0:
            return False, f"rclone archive to NAS failed: {res.stderr or res2.stderr}"

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items
            SET triage_status = 'archived_nas',
                target_path = ?,
                updated_at = ?
            WHERE id = ?
        """, (target_path, now_iso, item_id))

    return True, f"Converted and archived to NAS: {target_path}"


def delete_item_from_gdrive(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Delete the Google-native document from Google Drive (moves to Drive Trash via rclone deletefile)."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    rel_path = item["path"]
    cur_status = item["triage_status"]

    print(f"Deleting 'gdrive:{rel_path}' from Google Drive...")
    del_cmd = ["rclone", "--bind", "0.0.0.0", "deletefile", f"gdrive:{rel_path}"]
    res = subprocess.run(del_cmd, capture_output=True, text=True)
    if res.returncode != 0 and "not found" not in (res.stderr or "").lower():
        return False, f"rclone deletefile failed: {res.stderr}"

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

    return True, f"Deleted '{rel_path}' from Google Drive."


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
  <title>Google Docs &amp; Sheets Migration to Collabora</title>
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
      padding: 1rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    header h1 {
      margin: 0;
      font-size: 1.25rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .header-links {
      font-size: 0.9rem;
    }
    .header-links a {
      color: #93c5fd;
      text-decoration: none;
      margin-left: 1rem;
      font-weight: 500;
    }
    .header-links a:hover {
      text-decoration: underline;
    }
    .container {
      max-width: 1400px;
      margin: 1.25rem auto;
      padding: 0 1.5rem;
    }
    .info-banner {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1e3a8a;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1.25rem;
      font-size: 0.875rem;
      line-height: 1.45;
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
      font-size: 0.78rem;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 600;
    }
    .stat-card .val {
      font-size: 1.6rem;
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
      font-size: 0.9rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: white;
    }
    input[type="search"] {
      flex: 1;
      min-width: 240px;
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
      font-size: 0.9rem;
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
      z-index: 1000;
      font-size: 0.9rem;
    }
  </style>
</head>
<body>
  <header>
    <h1><span>🗂️</span> Google Docs &amp; Sheets Migration to Collabora</h1>
    <div class="header-links">
      <a href="__COLLABORA_WEB_URL__/" id="link-collabora" target="_blank">Collabora Editor ↗</a>
      <a href="https://drive.google.com/" target="_blank">Google Drive Web ↗</a>
    </div>
  </header>

  <div class="container">
    <div class="info-banner">
      <strong>Only Google-Native Documents Shown:</strong> PDFs, images, archives, and regular uploaded Excel/Word files are excluded — only proprietary Google Drive documents (Google Sheets, Docs, Slides) that cannot be opened without Google Drive tools are listed.
      Files are <strong>not converted yet</strong>; conversion to <code>.xlsx</code> / <code>.docx</code> / <code>.pptx</code> happens on-demand when you click <strong>✨ To Collabora</strong> or <strong>💾 To NAS</strong>.
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="label">Google-Native Docs</div>
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
      <label for="folder-select">Folder:</label>
      <select id="folder-select" onchange="loadItems()">
        <option value="all">All Folders</option>
      </select>
      <button class="btn-primary" onclick="rescanFolder()">🔄 Re-Scan Folder</button>

      <input type="search" id="search-box" placeholder="Instant search by name or folder..." oninput="debounceSearch()">

      <div class="filter-pills" id="category-pills">
        <div class="pill active" data-cat="all" onclick="setCategory('all')">All Google Docs</div>
        <div class="pill" data-cat="spreadsheet" onclick="setCategory('spreadsheet')">📊 Google Sheets</div>
        <div class="pill" data-cat="document" onclick="setCategory('document')">📝 Google Docs</div>
        <div class="pill" data-cat="presentation" onclick="setCategory('presentation')">📽️ Google Slides</div>
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
          <th>Google Document &amp; Folder Path</th>
          <th>Google Type &amp; Export Target</th>
          <th>Conversion State</th>
          <th>Modified</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="items-body">
        <tr><td colspan="6" style="text-align:center;padding:2rem;">Loading cached Google-native documents...</td></tr>
      </tbody>
    </table>
  </div>

  <div id="toast"></div>

  <script>
    let currentCategory = 'all';
    let currentStatus = 'all';
    let searchTimeout = null;

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
      sel.innerHTML = '<option value="all">All Folders (' + data.folders.length + ')</option>';
      for (const f of data.folders) {
        const opt = document.createElement('option');
        opt.value = f.folder;
        opt.innerText = f.folder + ' (' + f.total + ' docs, ' + f.pending + ' pending)';
        if (f.folder === cur) opt.selected = true;
        sel.appendChild(opt);
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
      searchTimeout = setTimeout(loadItems, 200);
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
        return '<span style="color:var(--danger);font-size:0.82rem">Removed from Google Drive</span>';
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
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#64748b">No matching Google-native documents found.</td></tr>';
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
            actionBtns += `<button class="btn-action delete" title="Delete original from Google Drive after verifying conversion" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete from Drive</button>`;
          }
        } else if (it.triage_status === 'archived_nas') {
          actionBtns += `<button class="btn-action collab" title="Also convert &amp; import to Collabora" onclick="doAction('${it.id}', 'collabora')">✨ To Collabora</button>`;
          if (!it.drive_deleted) {
            actionBtns += `<button class="btn-action delete" title="Delete original from Google Drive after verifying NAS archive" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete from Drive</button>`;
          }
        } else if (it.triage_status === 'deleted' || it.drive_deleted === 1) {
          actionBtns = `<span style="color:var(--text-muted);font-size:0.8rem">Deleted from Google Drive</span>`;
        } else {
          actionBtns = `
            <button class="btn-action collab" title="Convert &amp; import to Collabora" onclick="doAction('${it.id}', 'collabora')">✨ To Collabora</button>
            <button class="btn-action nas" title="Convert &amp; archive to NAS" onclick="doAction('${it.id}', 'nas')">💾 To NAS</button>
            <button class="btn-action delete" title="Directly delete this document from Google Drive" onclick="confirmDelete('${it.id}', '${safeNameJs}')">🗑 Delete</button>
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
      if (!confirm('Delete "' + title + '" from Google Drive (moves to Google Drive Trash)?')) {
        return;
      }
      doAction(id, 'delete');
    }

    async function doAction(id, act) {
      showToast('Processing: ' + act + '...');
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
      }
    }

    async function rescanFolder() {
      const folder = document.getElementById('folder-select').value;
      if (folder === 'all') {
        alert('Please select a specific folder (or "root") from the dropdown to re-scan from Google Drive.');
        return;
      }
      showToast('Scanning Google Drive folder "' + folder + '" for Google-native documents...');
      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: folder })
      });
      const data = await res.json();
      showToast(data.message);
      fetchStats();
      loadItems();
    }

    function showToast(msg, isError = false) {
      const toast = document.getElementById('toast');
      toast.innerText = msg;
      toast.style.background = isError ? 'var(--danger)' : '#1e293b';
      toast.style.display = 'block';
      setTimeout(() => { toast.style.display = 'none'; }, 4500);
    }

    function escapeHtml(s) {
      if (!s) return '';
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Init
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
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content: str, code: int = 200):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path == "/" or path == "/index.html":
            rendered = HTML_TEMPLATE.replace("__COLLABORA_WEB_URL__", html.escape(COLLABORA_WEB_URL))
            return self.send_html(rendered)

        if path == "/api/stats":
            conn = get_db(self.db_path)
            total = conn.execute("SELECT COUNT(*) FROM items WHERE is_dir = 0 AND size = -1").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'pending' AND drive_deleted = 0 AND is_dir = 0 AND size = -1"
            ).fetchone()[0]
            imported = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'imported_collabora' AND is_dir = 0 AND size = -1"
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_status = 'archived_nas' AND is_dir = 0 AND size = -1"
            ).fetchone()[0]
            deleted = conn.execute(
                "SELECT COUNT(*) FROM items WHERE (triage_status = 'deleted' OR drive_deleted = 1) AND is_dir = 0 AND size = -1"
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

        if path == "/api/action":
            item_id = payload.get("id")
            action = payload.get("action")
            if not item_id or not action:
                return self.send_json({"ok": False, "message": "Missing id or action"}, 400)

            try:
                if action == "collabora":
                    ok, msg = import_item_to_collabora(item_id, self.db_path)
                elif action == "nas":
                    ok, msg = archive_item_to_nas(item_id, self.db_path)
                elif action == "delete":
                    ok, msg = delete_item_from_gdrive(item_id, self.db_path)
                elif action == "pending":
                    ok, msg = update_item_status(item_id, "pending", self.db_path)
                else:
                    ok, msg = False, f"Unknown action: {action}"
            except Exception as exc:
                ok, msg = False, str(exc)

            return self.send_json({"ok": ok, "message": msg})

        if path == "/api/scan":
            folder = payload.get("folder", "")
            if not folder:
                return self.send_json({"ok": False, "message": "Missing folder"}, 400)
            count = index_folder(folder, self.db_path)
            return self.send_json({
                "ok": True,
                "message": f"Scanned {count} Google-native document(s) from folder '{folder}'.",
            })

        self.send_response(404)
        self.end_headers()


def serve(port: int = DEFAULT_PORT, db_path: str = DEFAULT_DB_PATH):
    get_db(db_path)
    TriageHandler.db_path = db_path
    server = ThreadingHTTPServer(("0.0.0.0", port), TriageHandler)
    print(f"Google Drive Triage UI serving on http://0.0.0.0:{port} (Collabora: {COLLABORA_WEB_URL}, DB: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Google Drive Interactive Migration & Triage Assistant")
    subparsers = parser.add_subparsers(dest="command")

    # index
    index_parser = subparsers.add_parser("index", help="Index a folder from Google Drive into SQLite (Google-native docs only)")
    index_parser.add_argument("folder", nargs="?", default="root", help="Folder name to index (e.g. 'root', 'huis', 'personal')")
    index_parser.add_argument("--depth", type=int, default=4, help="Max depth to recurse")
    index_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    # index-all
    index_all_parser = subparsers.add_parser("index-all", help="Index all top-level folders into SQLite (excluding backup/Postbox)")
    index_all_parser.add_argument(
        "--exclude",
        default=",".join(DEFAULT_EXCLUDES),
        help="Comma-separated folder names to exclude",
    )
    index_all_parser.add_argument("--depth", type=int, default=4, help="Max depth to recurse")
    index_all_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    # stats
    stats_parser = subparsers.add_parser("stats", help="Show database summary statistics")
    stats_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Run the Web UI server")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on (default: 8085)")
    serve_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    args = parser.parse_args()

    if args.command == "index":
        index_folder(args.folder, db_path=args.db, max_depth=args.depth)
    elif args.command == "index-all":
        excludes = [x.strip() for x in args.exclude.split(",") if x.strip()]
        index_all_folders(exclude_folders=excludes, db_path=args.db, max_depth=args.depth)
    elif args.command == "stats":
        folders = get_folders_summary(args.db)
        print(f"Google-Native Documents summary in database ({args.db}):")
        for f in folders:
            print(
                f"  {f['folder']:<25} Total: {f['total']:<4} Pending: {f['pending']:<4} "
                f"In Collabora: {f['imported']:<3} On NAS: {f['archived']:<3} Deleted: {f['deleted']}"
            )
    elif args.command == "serve":
        serve(port=args.port, db_path=args.db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
