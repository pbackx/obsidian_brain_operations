#!/usr/bin/env python3
"""
gdrive_triage.py - Interactive Google Drive Migration & Triage Assistant

Features:
- Caches Google Drive file metadata in a local SQLite database to prevent API rate limits.
- Provides folder-by-folder or recursive indexing via rclone (`gdrive:`).
- Categorizes files: spreadsheets, documents, presentations, PDFs, CAD drawings, images, archives.
- Web UI (vanilla HTML/JS/CSS, zero dependencies):
  * Filter by folder, category, and triage status.
  * Instant search across thousands of files (< 5ms).
  * 1-Click "Convert & Import to Collabora Online" (CT 104).
  * 1-Click "Archive to Synology NAS".
  * Mark as "Ignored" or "To Delete".
  * Direct Google Drive link for every file.
"""

import argparse
import datetime
import html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/gdrive_triage/gdrive_triage.db")
DEFAULT_PORT = 8085
COLLABORA_SSH_HOST = os.environ.get("COLLABORA_HOST", "127.0.0.1")
COLLABORA_SSH_USER = os.environ.get("COLLABORA_USER", "root")
COLLABORA_TARGET_DIR = os.environ.get("COLLABORA_DIR", "/srv/spreadsheets")
COLLABORA_WEB_URL = os.environ.get("COLLABORA_URL", "http://collabora:9980")
NAS_ARCHIVE_DIR = os.environ.get("NAS_ARCHIVE_DIR", os.path.expanduser("~/archive/google_drive"))


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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
    return conn


def categorize_item(name: str, mime: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    lower_name = name.lower()
    lower_mime = mime.lower()

    if "spreadsheet" in lower_mime or lower_name.endswith((".xlsx", ".xls", ".ods", ".csv")):
        return "spreadsheet"
    if "document" in lower_mime or "msword" in lower_mime or lower_name.endswith((".docx", ".doc", ".odt", ".rtf", ".txt")):
        return "document"
    if "presentation" in lower_mime or "powerpoint" in lower_mime or lower_name.endswith((".pptx", ".ppt", ".odp")):
        return "presentation"
    if "pdf" in lower_mime or lower_name.endswith(".pdf"):
        return "pdf"
    if "image" in lower_mime or lower_name.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp")):
        return "image"
    if "zip" in lower_mime or "tar" in lower_mime or "compressed" in lower_mime or lower_name.endswith((".zip", ".tar", ".gz", ".7z", ".rar")):
        return "archive"
    if "dwg" in lower_mime or lower_name.endswith((".dwg", ".dxf")):
        return "cad"
    return "other"


def index_folder(folder_name: str, db_path: str = DEFAULT_DB_PATH, max_depth: int = 4) -> int:
    """Scan a Google Drive folder via rclone lsjson and store in SQLite."""
    print(f"Indexing Google Drive folder: '{folder_name}' (max-depth {max_depth})...")
    remote_path = f"gdrive:{folder_name}" if folder_name else "gdrive:"
    cmd = ["rclone", "lsjson", remote_path, "--recursive", f"--max-depth={max_depth}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Error running rclone: {exc.stderr}", file=sys.stderr)
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
            name = it.get("Name", "")
            rel_path = it.get("Path", "")
            full_path = f"{folder_name}/{rel_path}" if folder_name else rel_path
            mime = it.get("MimeType", "")
            size = it.get("Size", -1)
            mod_time = it.get("ModTime", "")
            is_dir = 1 if it.get("IsDir", False) else 0
            category = categorize_item(name, mime, is_dir == 1)

            # Preserve existing triage status if already triaged
            existing = conn.execute("SELECT triage_status, target_path, notes FROM items WHERE id = ?", (item_id,)).fetchone()
            triage_status = existing["triage_status"] if existing else "pending"
            target_path = existing["target_path"] if existing else None
            notes = existing["notes"] if existing else None

            conn.execute("""
                INSERT INTO items (id, path, name, folder, is_dir, mime_type, size, mod_time, category, triage_status, target_path, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    name = excluded.name,
                    folder = excluded.folder,
                    is_dir = excluded.is_dir,
                    mime_type = excluded.mime_type,
                    size = excluded.size,
                    mod_time = excluded.mod_time,
                    category = excluded.category,
                    updated_at = excluded.updated_at
            """, (item_id, full_path, name, folder_name or "root", is_dir, mime, size, mod_time, category, triage_status, target_path, notes, now_iso))
            count += 1

        conn.execute("""
            INSERT INTO folders (name, last_scanned, item_count)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_scanned = excluded.last_scanned,
                item_count = excluded.item_count
        """, (folder_name or "root", now_iso, count))

    print(f"Successfully indexed {count} items in folder '{folder_name}'.")
    return count


def list_top_folders() -> List[str]:
    """List top-level directories on gdrive:"""
    cmd = ["rclone", "lsd", "gdrive:", "--max-depth=1"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    folders = []
    for line in res.stdout.strip().splitlines():
        parts = line.split()
        if parts:
            folder_name = parts[-1]
            folders.append(folder_name)
    return folders


def index_all_folders(exclude_folders: Optional[List[str]] = None, db_path: str = DEFAULT_DB_PATH, max_depth: int = 4):
    excludes = set(exclude_folders or ["backup", "Postbox"])
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
               SUM(CASE WHEN triage_status = 'pending' AND is_dir = 0 THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN triage_status = 'imported_collabora' THEN 1 ELSE 0 END) as imported,
               SUM(CASE WHEN triage_status = 'archived_nas' THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN category IN ('spreadsheet', 'document', 'presentation') AND is_dir = 0 THEN 1 ELSE 0 END) as office_count
        FROM items
        GROUP BY folder
        ORDER BY total DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_items(db_path: str = DEFAULT_DB_PATH, folder: Optional[str] = None, category: Optional[str] = None,
              status: Optional[str] = None, query: Optional[str] = None, limit: int = 250) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    clauses = ["is_dir = 0"]
    params: List[Any] = []

    if folder and folder != "all":
        clauses.append("folder = ?")
        params.append(folder)
    if category and category != "all":
        clauses.append("category = ?")
        params.append(category)
    if status and status != "all":
        clauses.append("triage_status = ?")
        params.append(status)
    if query:
        clauses.append("(name LIKE ? OR path LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    sql = f"SELECT * FROM items WHERE {' AND '.join(clauses)} ORDER BY mod_time DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def import_item_to_collabora(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Export file from Google Drive via rclone and deploy to CT 104 /srv/spreadsheets/."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    rel_path = item["path"]
    folder = item["folder"]
    name = item["name"]
    category = item["category"]

    # Determine file extension if converting
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        if category == "spreadsheet":
            ext = ".xlsx"
            name = name + ".xlsx"
        elif category == "document":
            ext = ".docx"
            name = name + ".docx"
        elif category == "presentation":
            ext = ".pptx"
            name = name + ".pptx"

    local_tmp_dir = os.path.expanduser("~/.cache/gdrive_triage/export")
    os.makedirs(local_tmp_dir, exist_ok=True)
    local_tmp_file = os.path.join(local_tmp_dir, name)

    # Export / copy from Google Drive
    print(f"Exporting 'gdrive:{rel_path}' to temporary file '{local_tmp_file}'...")
    copy_cmd = ["rclone", "copyto", f"gdrive:{rel_path}", local_tmp_file]
    res = subprocess.run(copy_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"rclone copy failed: {res.stderr}"

    # SCP to CT 104
    target_subfolder = f"{COLLABORA_TARGET_DIR}/{folder}" if folder != "root" else COLLABORA_TARGET_DIR
    target_remote_path = f"{target_subfolder}/{name}"

    mkdir_cmd = ["ssh", "-o", "BatchMode=yes", f"{COLLABORA_SSH_USER}@{COLLABORA_SSH_HOST}", f"mkdir -p '{target_subfolder}'"]
    subprocess.run(mkdir_cmd, check=True)

    scp_cmd = ["scp", "-o", "BatchMode=yes", local_tmp_file, f"{COLLABORA_SSH_USER}@{COLLABORA_SSH_HOST}:{target_remote_path}"]
    scp_res = subprocess.run(scp_cmd, capture_output=True, text=True)
    if scp_res.returncode != 0:
        return False, f"SCP to CT 104 failed: {scp_res.stderr}"

    chown_cmd = ["ssh", "-o", "BatchMode=yes", f"{COLLABORA_SSH_USER}@{COLLABORA_SSH_HOST}", f"chown wopi:wopi '{target_remote_path}'"]
    subprocess.run(chown_cmd, check=True)

    # Clean local tmp
    if os.path.exists(local_tmp_file):
        os.remove(local_tmp_file)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items 
            SET triage_status = 'imported_collabora', 
                target_path = ?,
                updated_at = ?
            WHERE id = ?
        """, (target_remote_path, now_iso, item_id))

    return True, f"Successfully imported to CT 104: {target_remote_path}"


def archive_item_to_nas(item_id: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    """Copy file to Synology NAS archive directory."""
    conn = get_db(db_path)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return False, "Item not found"

    rel_path = item["path"]
    folder = item["folder"]
    name = item["name"]

    target_dir = os.path.join(NAS_ARCHIVE_DIR, folder if folder != "root" else "")
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, name)

    copy_cmd = ["rclone", "copyto", f"gdrive:{rel_path}", target_path]
    res = subprocess.run(copy_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"rclone archive to NAS failed: {res.stderr}"

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE items 
            SET triage_status = 'archived_nas', 
                target_path = ?,
                updated_at = ?
            WHERE id = ?
        """, (target_path, now_iso, item_id))

    return True, f"Successfully archived to NAS: {target_path}"


def update_item_status(item_id: str, status: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[bool, str]:
    conn = get_db(db_path)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        conn.execute("UPDATE items SET triage_status = ?, updated_at = ? WHERE id = ?", (status, now_iso, item_id))
    return True, f"Status updated to {status}"


# ----------------------------------------------------------------------
# WEB SERVER & UI
# ----------------------------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Google Drive Interactive Migration &amp; Triage</title>
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
    }
    .container {
      max-width: 1400px;
      margin: 1.5rem auto;
      padding: 0 1.5rem;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .stat-card {
      background: var(--card-bg);
      padding: 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .stat-card .label {
      font-size: 0.8rem;
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
      margin-bottom: 1.5rem;
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
      min-width: 250px;
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
      padding: 0.3rem 0.6rem;
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
      text-transform: capitalize;
    }
    .badge-pending { background: #fef3c7; color: #92400e; }
    .badge-imported_collabora { background: #dcfce7; color: #166534; }
    .badge-archived_nas { background: #e0e7ff; color: #3730a3; }
    .badge-ignored { background: #f1f5f9; color: #475569; }
    .badge-to_delete { background: #fee2e2; color: #991b1b; }
    .badge-cat {
      background: #f1f5f9;
      color: #334155;
      font-size: 0.75rem;
      border: 1px solid #cbd5e1;
    }
    .btn-group {
      display: flex;
      gap: 0.3rem;
    }
    .btn-action {
      padding: 0.25rem 0.5rem;
      font-size: 0.75rem;
      border-radius: 4px;
      border: 1px solid var(--border);
      cursor: pointer;
      background: white;
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
    .btn-action.ignore {
      color: var(--text-muted);
    }
    .file-name {
      font-weight: 500;
      word-break: break-all;
    }
    .file-path {
      font-size: 0.75rem;
      color: var(--text-muted);
      display: block;
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
    <h1><span>🗂️</span> Google Drive Interactive Migration &amp; Triage</h1>
    <div class="header-links">
      <a href="#" id="link-collabora" target="_blank">Collabora Editor ↗</a>
      <a href="https://drive.google.com/" target="_blank">Google Drive Web ↗</a>
    </div>
  </header>

  <div class="container">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="label">Total Cached Files</div>
        <div class="val" id="stat-total">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Pending Review</div>
        <div class="val" id="stat-pending" style="color:var(--warning)">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Imported to Collabora</div>
        <div class="val" id="stat-imported" style="color:var(--success)">-</div>
      </div>
      <div class="stat-card">
        <div class="label">Archived to NAS</div>
        <div class="val" id="stat-archived" style="color:var(--purple)">-</div>
      </div>
    </div>

    <div class="toolbar">
      <label for="folder-select">Folder:</label>
      <select id="folder-select" onchange="loadItems()">
        <option value="all">All Folders</option>
      </select>
      <button class="btn-primary" onclick="rescanFolder()">🔄 Re-Scan Folder</button>

      <input type="search" id="search-box" placeholder="Instant search by name or path..." oninput="debounceSearch()">

      <div class="filter-pills" id="category-pills">
        <div class="pill active" data-cat="all" onclick="setCategory('all')">All Types</div>
        <div class="pill" data-cat="spreadsheet" onclick="setCategory('spreadsheet')">📊 Spreadsheets</div>
        <div class="pill" data-cat="document" onclick="setCategory('document')">📝 Docs</div>
        <div class="pill" data-cat="pdf" onclick="setCategory('pdf')">📑 PDFs</div>
        <div class="pill" data-cat="image" onclick="setCategory('image')">🖼 Images</div>
        <div class="pill" data-cat="archive" onclick="setCategory('archive')">📦 Archives</div>
        <div class="pill" data-cat="cad" onclick="setCategory('cad')">📐 CAD</div>
      </div>

      <div class="filter-pills" id="status-pills">
        <div class="pill active" data-status="all" onclick="setStatus('all')">All Statuses</div>
        <div class="pill" data-status="pending" onclick="setStatus('pending')">Pending</div>
        <div class="pill" data-status="imported_collabora" onclick="setStatus('imported_collabora')">Imported</div>
        <div class="pill" data-status="archived_nas" onclick="setStatus('archived_nas')">Archived</div>
        <div class="pill" data-status="ignored" onclick="setStatus('ignored')">Ignored</div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th>File &amp; Path</th>
          <th>Type</th>
          <th>Size</th>
          <th>Modified</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="items-body">
        <tr><td colspan="6" style="text-align:center;padding:2rem;">Loading cached items...</td></tr>
      </tbody>
    </table>
  </div>

  <div id="toast"></div>

  <script>
    let currentCategory = 'all';
    let currentStatus = 'all';
    let searchTimeout = null;
    const collaboraUrl = window.location.protocol + '//' + window.location.hostname.replace(/:\d+$/, '') + ':9980';
    document.getElementById('link-collabora').href = 'http://' + window.location.hostname + ':80';

    async function fetchStats() {
      const res = await fetch('/api/stats');
      const data = await res.json();
      document.getElementById('stat-total').innerText = data.total;
      document.getElementById('stat-pending').innerText = data.pending;
      document.getElementById('stat-imported').innerText = data.imported;
      document.getElementById('stat-archived').innerText = data.archived;

      const sel = document.getElementById('folder-select');
      const cur = sel.value;
      sel.innerHTML = '<option value="all">All Folders (' + data.folders.length + ')</option>';
      for (const f of data.folders) {
        const opt = document.createElement('option');
        opt.value = f.folder;
        opt.innerText = f.folder + ' (' + f.total + ' items, ' + f.pending + ' pending)';
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

    function formatSize(sz) {
      if (sz === -1 || sz === null) return '<span style="color:#64748b">Google Doc</span>';
      if (sz < 1024) return sz + ' B';
      if (sz < 1048576) return (sz / 1024).toFixed(1) + ' KB';
      return (sz / 1048576).toFixed(1) + ' MB';
    }

    function formatDate(dt) {
      if (!dt) return '-';
      return dt.substring(0, 10);
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
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#64748b">No matching files found.</td></tr>';
        return;
      }

      let htmlRows = '';
      for (const it of items) {
        const driveUrl = 'https://drive.google.com/open?id=' + encodeURIComponent(it.id);
        const statusBadge = '<span class="badge badge-' + it.triage_status + '">' + it.triage_status.replace('_', ' ') + '</span>';
        const catBadge = '<span class="badge badge-cat">' + it.category + '</span>';

        let actionBtns = `
          <button class="btn-action collab" title="Convert & Export to Collabora (CT 104)" onclick="doAction('${it.id}', 'collabora')">✨ To Collabora</button>
          <button class="btn-action nas" title="Archive to Synology NAS" onclick="doAction('${it.id}', 'nas')">💾 To NAS</button>
          <button class="btn-action ignore" title="Mark as Ignored" onclick="doAction('${it.id}', 'ignore')">👁 Ignore</button>
        `;

        if (it.triage_status === 'imported_collabora') {
          actionBtns = `<span style="color:var(--success);font-size:0.8rem;font-weight:600">✓ On CT 104</span>
                        <a href="${collaboraUrl}" target="_blank" class="btn-action collab">Open in Collabora ↗</a>`;
        } else if (it.triage_status === 'archived_nas') {
          actionBtns = `<span style="color:var(--purple);font-size:0.8rem;font-weight:600">✓ On NAS</span>
                        <button class="btn-action" onclick="doAction('${it.id}', 'pending')">Reset</button>`;
        }

        htmlRows += `
          <tr>
            <td>
              <a href="${driveUrl}" target="_blank" class="file-name" title="Open in Google Drive">${escapeHtml(it.name)}</a>
              <span class="file-path">${escapeHtml(it.path)}</span>
            </td>
            <td>${catBadge}</td>
            <td>${formatSize(it.size)}</td>
            <td>${formatDate(it.mod_time)}</td>
            <td>${statusBadge}</td>
            <td><div class="btn-group">${actionBtns}</div></td>
          </tr>
        `;
      }
      tbody.innerHTML = htmlRows;
    }

    async function doAction(id, act) {
      showToast('Processing action: ' + act + '...');
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
        alert('Please select a specific folder to re-scan from Google Drive.');
        return;
      }
      showToast('Scanning Google Drive folder: ' + folder + ' (this takes a few seconds)...');
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
      setTimeout(() => { toast.style.display = 'none'; }, 4000);
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
            return self.send_html(HTML_TEMPLATE)

        if path == "/api/stats":
            conn = get_db(self.db_path)
            total = conn.execute("SELECT COUNT(*) FROM items WHERE is_dir = 0").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM items WHERE triage_status = 'pending' AND is_dir = 0").fetchone()[0]
            imported = conn.execute("SELECT COUNT(*) FROM items WHERE triage_status = 'imported_collabora'").fetchone()[0]
            archived = conn.execute("SELECT COUNT(*) FROM items WHERE triage_status = 'archived_nas'").fetchone()[0]
            folders = get_folders_summary(self.db_path)
            return self.send_json({
                "total": total,
                "pending": pending,
                "imported": imported,
                "archived": archived,
                "folders": folders
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

            if action == "collabora":
                ok, msg = import_item_to_collabora(item_id, self.db_path)
            elif action == "nas":
                ok, msg = archive_item_to_nas(item_id, self.db_path)
            elif action in ("ignore", "ignored"):
                ok, msg = update_item_status(item_id, "ignored", self.db_path)
            elif action == "pending":
                ok, msg = update_item_status(item_id, "pending", self.db_path)
            elif action == "delete":
                ok, msg = update_item_status(item_id, "to_delete", self.db_path)
            else:
                ok, msg = False, f"Unknown action: {action}"

            return self.send_json({"ok": ok, "message": msg})

        if path == "/api/scan":
            folder = payload.get("folder", "")
            if not folder:
                return self.send_json({"ok": False, "message": "Missing folder"}, 400)
            count = index_folder(folder, self.db_path)
            return self.send_json({"ok": True, "message": f"Scanned {count} items from Google Drive folder '{folder}'."})

        self.send_response(404)
        self.end_headers()


def serve(port: int = DEFAULT_PORT, db_path: str = DEFAULT_DB_PATH):
    TriageHandler.db_path = db_path
    server = ThreadingHTTPServer(("0.0.0.0", port), TriageHandler)
    print(f"Google Drive Triage UI serving on http://0.0.0.0:{port} (SQLite DB: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Google Drive Interactive Migration & Triage Assistant")
    subparsers = parser.add_subparsers(dest="command")

    # index
    index_parser = subparsers.add_parser("index", help="Index a folder from Google Drive into SQLite")
    index_parser.add_argument("folder", nargs="?", default="", help="Folder name to index (e.g. 'huis', 'personal')")
    index_parser.add_argument("--depth", type=int, default=4, help="Max depth to recurse")
    index_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")

    # index-all
    index_all_parser = subparsers.add_parser("index-all", help="Index all top-level folders into SQLite (excluding backup/Postbox)")
    index_all_parser.add_argument("--exclude", default="backup,Postbox", help="Comma-separated folder names to exclude")
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
        print(f"Folders summary in database ({args.db}):")
        for f in folders:
            print(f"  {f['folder']:<25} Total: {f['total']:<4} Office: {f['office_count']:<3} Pending: {f['pending']:<4} Imported: {f['imported']}")
    elif args.command == "serve":
        serve(port=args.port, db_path=args.db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
