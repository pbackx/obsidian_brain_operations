#!/usr/bin/env python3
"""
freewrite_ingest.py - Ingest drafts from Astrohaus Freewrite Postbox (Google Drive / NAS / local)
into an Obsidian Vault and prepare actionable #ai tasks for the Nightly Operator.

Features:
- Supports Postbox directory hierarchy (Postbox/A, Postbox/B, Postbox/C) or flat folders.
- Parses .txt, .md, and .docx files natively using only the Python standard library
  (extracts paragraphs from .docx via zipfile + xml.etree.ElementTree with zero pip dependencies).
- Normalizes ergonomic Freewrite task shorthand into standard Obsidian checkboxes:
    * "- [ ] Task #ai"      -> kept as-is
    * "- Task #ai"          -> "- [ ] Task #ai"
    * "AI: Task"            -> "- [ ] Task #ai"
    * "TODO: Task #ai"      -> "- [ ] Task #ai"
    * Standalone "#ai" draft or draft in designated --ai-folders (e.g. Folder C)
      -> wraps the note with a top-level "- [ ] Review Freewrite draft: <Title> #ai" task
         so vault_tool.py and the nightly operator process the entire brain dump.
- Idempotent SHA-256 state tracking (.freewrite_state.json) so source files in Google Drive
  can remain untouched (preventing one-way Postbox sync conflicts) while avoiding duplicates.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

SUPPORTED_EXTENSIONS = {'.txt', '.md', '.docx'}

AI_TAG_REGEX = re.compile(r'#ai(?:/[a-zA-Z0-9_\-]+)?\b', re.IGNORECASE)
CHECKBOX_REGEX = re.compile(r'^(\s*)- \[[ xX\t]\]\s+(.*)$')
BULLET_AI_REGEX = re.compile(r'^(\s*)[-*]\s+(?!\[[ xX\t]\])(.+#ai(?:/[a-zA-Z0-9_\-]+)?\b.*)$', re.IGNORECASE)
PREFIX_AI_REGEX = re.compile(r'^(\s*)(?:AI|TODO-AI|ASK-AI)\s*:\s*(.+)$', re.IGNORECASE)
TODO_PREFIX_WITH_TAG_REGEX = re.compile(r'^(\s*)TODO\s*:\s*(.+#ai(?:/[a-zA-Z0-9_\-]+)?\b.*)$', re.IGNORECASE)
STANDALONE_AI_LINE_REGEX = re.compile(r'^\s*#ai(?:/[a-zA-Z0-9_\-]+)?\s*$', re.IGNORECASE)


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def extract_text_from_docx(file_path: Path) -> str:
    """Extracts plain text paragraphs from a .docx file using only Python stdlib."""
    paragraphs: List[str] = []
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            if 'word/document.xml' not in zf.namelist():
                return ""
            xml_bytes = zf.read('word/document.xml')
        root = ET.fromstring(xml_bytes)
        # WordprocessingML namespace
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
            texts: List[str] = []
            for elem in p.iter():
                if elem.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t' and elem.text:
                    texts.append(elem.text)
                elif elem.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br':
                    texts.append('\n')
                elif elem.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab':
                    texts.append('\t')
            para_text = ''.join(texts).rstrip()
            paragraphs.append(para_text)
    except Exception as e:
        print(f"Warning: Failed to parse DOCX {file_path}: {e}", file=sys.stderr)
        return ""

    # Collapse excessive blank paragraphs while preserving paragraph breaks
    cleaned: List[str] = []
    blank_count = 0
    for p in paragraphs:
        if not p.strip():
            blank_count += 1
            if blank_count <= 1:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(p)
    return '\n'.join(cleaned).strip()


def read_draft_content(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext == '.docx':
        return extract_text_from_docx(file_path)
    try:
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as f:
            content = f.read()
        return content.replace('\r\n', '\n').replace('\r', '\n').strip()
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return ""


def sanitize_filename(name: str, max_length: int = 70) -> str:
    # Strip markdown heading markers, tags, and invalid filesystem characters
    clean = re.sub(r'^#+\s*', '', name).strip()
    clean = AI_TAG_REGEX.sub('', clean).strip()
    clean = re.sub(r'^(?:AI|TODO-AI|ASK-AI|TODO)\s*:\s*', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'^- \[[ xX\t]\]\s*', '', clean).strip()
    clean = re.sub(r'[\\/*?:"<>|\[\]#^]', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip(' .-')
    if len(clean) > max_length:
        truncated = clean[:max_length].rsplit(' ', 1)[0]
        clean = truncated if truncated else clean[:max_length]
    return clean or "Freewrite Draft"


def derive_title_and_body(raw_text: str, fallback_stem: str) -> Tuple[str, str]:
    lines = raw_text.split('\n')
    non_empty_indices = [i for i, l in enumerate(lines) if l.strip()]
    if not non_empty_indices:
        return sanitize_filename(fallback_stem), ""

    first_idx = non_empty_indices[0]
    first_line = lines[first_idx].strip()

    # If the first line is a standalone "#ai" marker, look at the next non-empty line for title
    if STANDALONE_AI_LINE_REGEX.match(first_line) and len(non_empty_indices) > 1:
        second_idx = non_empty_indices[1]
        candidate = lines[second_idx].strip()
        title = sanitize_filename(candidate)
        return title, raw_text

    # If the first line is a markdown heading (# Title), use it as title and strip from body
    if first_line.startswith('#'):
        title = sanitize_filename(first_line)
        remaining = '\n'.join(lines[first_idx + 1:]).strip()
        return title, remaining if remaining else first_line

    # If the first line is short (<= 80 chars) and followed by a blank line, treat as title
    if len(first_line) <= 80 and not CHECKBOX_REGEX.match(first_line) and not PREFIX_AI_REGEX.match(first_line):
        if len(lines) == first_idx + 1 or not lines[first_idx + 1].strip():
            title = sanitize_filename(first_line)
            remaining = '\n'.join(lines[first_idx + 1:]).strip()
            if remaining:
                return title, remaining

    # Otherwise derive title from first sentence/clause or filename
    first_sentence = re.split(r'[.!?]\s+', first_line, maxsplit=1)[0]
    title = sanitize_filename(first_sentence if len(first_sentence) >= 4 else fallback_stem)
    return title, raw_text


def normalize_freewrite_tasks(body: str, title: str, force_ai_task: bool = False) -> Tuple[str, int]:
    """
    Normalizes Freewrite shorthand into standard Obsidian '- [ ] ... #ai' checkboxes.
    Returns (normalized_markdown_body, count_of_ai_tasks).
    """
    lines = body.split('\n')
    out_lines: List[str] = []
    ai_task_count = 0
    has_any_ai_tag = bool(AI_TAG_REGEX.search(body))
    standalone_ai_tags: List[str] = []

    for line in lines:
        # 1. Existing checkbox with #ai
        cb_match = CHECKBOX_REGEX.match(line)
        if cb_match:
            if AI_TAG_REGEX.search(line):
                ai_task_count += 1
            out_lines.append(line)
            continue

        # 2. Standalone "#ai" or "#ai/focus" on its own line (used to tag an entire draft)
        if STANDALONE_AI_LINE_REGEX.match(line):
            standalone_ai_tags.append(line.strip())
            continue

        # 3. Prefix shorthand: "AI: Do something"
        prefix_match = PREFIX_AI_REGEX.match(line)
        if prefix_match:
            indent, task_text = prefix_match.group(1), prefix_match.group(2).strip()
            if not AI_TAG_REGEX.search(task_text):
                task_text = f"{task_text} #ai"
            out_lines.append(f"{indent}- [ ] {task_text}")
            ai_task_count += 1
            continue

        # 4. "TODO: something #ai"
        todo_match = TODO_PREFIX_WITH_TAG_REGEX.match(line)
        if todo_match:
            indent, task_text = todo_match.group(1), todo_match.group(2).strip()
            out_lines.append(f"{indent}- [ ] {task_text}")
            ai_task_count += 1
            continue

        # 5. Bullet with #ai but without checkbox brackets: "- something #ai"
        bullet_match = BULLET_AI_REGEX.match(line)
        if bullet_match:
            indent, task_text = bullet_match.group(1), bullet_match.group(2).strip()
            out_lines.append(f"{indent}- [ ] {task_text}")
            ai_task_count += 1
            continue

        out_lines.append(line)

    normalized_body = '\n'.join(out_lines).strip()

    # If the user tagged the draft as a whole (or wrote in a designated AI folder like Folder C)
    # without creating a specific checkbox line, wrap the draft with a top-level #ai checkbox
    # so vault_tool.py and obsidian-nightly-operator automatically triage the entire note.
    if ai_task_count == 0 and (force_ai_task or has_any_ai_tag or standalone_ai_tags):
        chosen_tag = standalone_ai_tags[0] if standalone_ai_tags else "#ai"
        # Strip inline #ai from prose if we are promoting the whole draft to a top-level task,
        # or keep it in prose; vault_tool.py only matches lines starting with `- [ ]`.
        header_task = (
            f"- [ ] Freewrite brain dump: {title} {chosen_tag}\n"
            f"  - Drafted on the Freewrite Smart Typewriter. Full text below in this note."
        )
        normalized_body = f"{header_task}\n\n---\n\n{normalized_body}" if normalized_body else header_task
        ai_task_count = 1

    return normalized_body, ai_task_count


def detect_postbox_folder(source_root: Path, file_path: Path) -> str:
    try:
        rel = file_path.relative_to(source_root)
        if len(rel.parts) > 1:
            top_folder = rel.parts[0].upper()
            if top_folder in {'A', 'B', 'C', 'FOLDER A', 'FOLDER B', 'FOLDER C'}:
                return top_folder.replace('FOLDER ', '')
            return rel.parts[0]
    except ValueError:
        pass
    return "Root"


def load_state(state_file: Path) -> Dict[str, Any]:
    if state_file.exists():
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read state file {state_file}: {e}", file=sys.stderr)
    return {"files": {}}


def save_state(state_file: Path, state: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)


def ingest_directory(
    source_dir: Path,
    vault_dir: Path,
    vault_subdir: str = "Freewrite",
    folders_filter: Optional[set] = None,
    ai_folders: Optional[set] = None,
    state_file: Optional[Path] = None,
    dry_run: bool = False
) -> List[Dict[str, Any]]:
    if state_file is None:
        state_file = vault_dir / "Brain_Operations" / ".freewrite_state.json"

    state = load_state(state_file)
    tracked_files: Dict[str, Any] = state.setdefault("files", {})
    results: List[Dict[str, Any]] = []

    if not source_dir.exists() or not source_dir.is_dir():
        print(f"Freewrite source directory not found or not mounted: {source_dir}", file=sys.stderr)
        return results

    target_base = vault_dir / vault_subdir

    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith('.')]
        for fname in sorted(files):
            if fname.startswith('.') or fname.startswith('~$'):
                continue
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            folder_slot = detect_postbox_folder(source_dir, fpath)
            if folders_filter and folder_slot.upper() not in folders_filter:
                continue

            rel_source_key = str(fpath.relative_to(source_dir))
            file_hash = compute_sha256(fpath)

            prev_entry = tracked_files.get(rel_source_key)
            if prev_entry and prev_entry.get("sha256") == file_hash:
                # Unchanged since last ingestion
                continue

            raw_text = read_draft_content(fpath)
            if not raw_text.strip():
                continue

            mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
            date_str = mtime.strftime('%Y-%m-%d')
            iso_ts = mtime.strftime('%Y-%m-%d %H:%M')

            title, body = derive_title_and_body(raw_text, fpath.stem)
            force_ai = bool(ai_folders and folder_slot.upper() in ai_folders)
            normalized_body, ai_tasks = normalize_freewrite_tasks(body, title, force_ai_task=force_ai)

            # Determine target path in vault
            if prev_entry and prev_entry.get("vault_rel_path"):
                vault_rel_path = prev_entry["vault_rel_path"]
                target_file = vault_dir / vault_rel_path
            else:
                safe_name = f"{date_str} - {title}.md"
                target_file = target_base / safe_name
                # Avoid colliding with a different source file
                counter = 2
                while target_file.exists() and (not prev_entry or prev_entry.get("vault_rel_path") != str(target_file.relative_to(vault_dir))):
                    target_file = target_base / f"{date_str} - {title} ({counter}).md"
                    counter += 1
                vault_rel_path = str(target_file.relative_to(vault_dir))

            note_markdown = (
                f"---\n"
                f"title: \"{title}\"\n"
                f"source: freewrite\n"
                f"freewrite_folder: \"{folder_slot}\"\n"
                f"freewrite_file: \"{rel_source_key}\"\n"
                f"created: {date_str}\n"
                f"synced: {iso_ts}\n"
                f"---\n\n"
                f"# {title}\n\n"
                f"{normalized_body}\n"
            )

            action = "created"
            if target_file.exists():
                existing_content = target_file.read_text(encoding='utf-8', errors='replace')
                # Check if the note was already processed by the nightly operator (#ai/done or #ai/question)
                if re.search(r'#ai/(?:done|question)\b', existing_content, re.IGNORECASE):
                    # Preserve the AI-updated header/links and append updated Freewrite content
                    action = "updated_followup"
                    prev_raw = prev_entry.get("raw_text", "") if prev_entry else ""
                    new_addition = raw_text[len(prev_raw):].strip() if prev_raw and raw_text.startswith(prev_raw) else raw_text
                    if new_addition:
                        # Re-open #ai/question -> #ai if user added follow-up remarks on the Freewrite
                        updated_existing = re.sub(r'#ai/question\b', '#ai', existing_content, flags=re.IGNORECASE)
                        note_markdown = (
                            f"{updated_existing.rstrip()}\n\n"
                            f"## Freewrite Follow-Up ({iso_ts})\n\n"
                            f"{new_addition}\n"
                        )
                    else:
                        note_markdown = existing_content
                else:
                    action = "updated"

            if not dry_run:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(note_markdown, encoding='utf-8')
                tracked_files[rel_source_key] = {
                    "sha256": file_hash,
                    "vault_rel_path": vault_rel_path,
                    "folder": folder_slot,
                    "title": title,
                    "ai_tasks": ai_tasks,
                    "ingested_at": datetime.now().isoformat(timespec='seconds'),
                    "raw_text": raw_text
                }

            results.append({
                "action": action,
                "source_file": rel_source_key,
                "vault_file": vault_rel_path,
                "folder": folder_slot,
                "title": title,
                "ai_tasks": ai_tasks
            })

    if not dry_run and results:
        save_state(state_file, state)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Freewrite Postbox drafts (from Google Drive / NAS / local folder) into Obsidian Vault."
    )
    parser.add_argument("--source", required=True, help="Path to local or mounted Postbox folder (containing A, B, C or draft files)")
    parser.add_argument("--vault", required=True, help="Path to local Obsidian vault")
    parser.add_argument("--subdir", default="Freewrite", help="Target subdirectory inside the Obsidian vault (default: Freewrite)")
    parser.add_argument("--folders", default="", help="Comma-separated Postbox folders to ingest (e.g., 'A,B,C' or 'C'; default: all)")
    parser.add_argument("--ai-folders", default="", help="Comma-separated Postbox folders where every draft is automatically queued as an #ai task even without an explicit #ai tag (e.g., 'C')")
    parser.add_argument("--dry-run", action="store_true", help="Preview ingestion without writing files")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()
    source_dir = Path(args.source).expanduser().resolve()
    vault_dir = Path(args.vault).expanduser().resolve()

    folders_filter = {f.strip().upper() for f in args.folders.split(',') if f.strip()} if args.folders else None
    ai_folders = {f.strip().upper() for f in args.ai_folders.split(',') if f.strip()} if args.ai_folders else None

    results = ingest_directory(
        source_dir=source_dir,
        vault_dir=vault_dir,
        vault_subdir=args.subdir,
        folders_filter=folders_filter,
        ai_folders=ai_folders,
        dry_run=args.dry_run
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("No new or modified Freewrite drafts to ingest.")
        else:
            mode_str = "[DRY-RUN] " if args.dry_run else ""
            print(f"{mode_str}Ingested {len(results)} Freewrite draft(s):")
            for r in results:
                print(f"  - [{r['action'].upper()}] {r['source_file']} -> {r['vault_file']} (Folder: {r['folder']}, #ai tasks: {r['ai_tasks']})")


if __name__ == "__main__":
    main()
