#!/usr/bin/env python3
"""
vault_tool.py - Fast CLI utility for discovering, inspecting, and managing AI tasks
in an Obsidian vault with Living Dossier support.

Features:
- Scans all Markdown files in the vault (excluding .obsidian and internal dirs).
- Finds unchecked markdown tasks with #ai tags (- [ ] ... #ai).
- Detects existing Living Dossier links inline or in nested bullets.
- Parses nested user notes/remarks under each task.
- Outputs structured JSON or formatted summary for Antigravity agents.
- Updates task checkbox with inline dossier link [[Dossier|📄]] and maintains
  a single consolidated 'Latest Update' line without duplicate history.
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

AI_TAG_REGEX = re.compile(r'#ai(?:\/[a-zA-Z0-9_\-]+)?\b', re.IGNORECASE)
CHECKBOX_REGEX = re.compile(r'^(\s*)- \[[ \t]\]\s+(.*)$')
DOSSIER_LINK_REGEX = re.compile(r'\[\[(Brain_Operations/Dossiers/[^\]|]+)(?:\|[^\]]+)?\]\]', re.IGNORECASE)
AI_UPDATE_BULLET_REGEX = re.compile(r'^\s*[-*]\s*\*\*(?:AI Update|Latest Update|Question for Peter)\b.*?\*\*', re.IGNORECASE)

DEFAULT_EXCLUDES = {'.obsidian', '.git', '.trash', 'Brain_Operations', 'node_modules', '.agents'}

def scan_vault(vault_path: Path, excludes: Optional[set] = None) -> List[Dict[str, Any]]:
    if excludes is None:
        excludes = DEFAULT_EXCLUDES

    tasks: List[Dict[str, Any]] = []

    if not vault_path.exists() or not vault_path.is_dir():
        print(f"Error: Vault path does not exist: {vault_path}", file=sys.stderr)
        return tasks

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in dirs if d not in excludes and not d.startswith('.')]

        rel_root = Path(root).relative_to(vault_path)
        if any(part in excludes for part in rel_root.parts):
            continue

        for file in files:
            if not file.endswith('.md'):
                continue

            file_path = Path(root) / file
            rel_file_path = file_path.relative_to(vault_path)

            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
            except Exception as e:
                print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
                continue

            i = 0
            while i < len(lines):
                line = lines[i]
                match = CHECKBOX_REGEX.match(line)
                if match:
                    indent, content = match.group(1), match.group(2)
                    tags = AI_TAG_REGEX.findall(content)
                    tag_names = [t.lower() for t in tags]
                    
                    # Ignore completed or paused tasks
                    if any(t in ['#ai/done', '#ai/paused', '#ai/pause', '#ai/stop'] for t in tag_names):
                        i += 1
                        continue

                    if tags:
                        title_clean = AI_TAG_REGEX.sub('', content).strip()
                        # Also strip existing dossier link for clean title if present inline
                        title_clean = DOSSIER_LINK_REGEX.sub('', title_clean).strip()
                        line_number = i + 1

                        # Check for existing dossier link on the task line
                        dossier_match = DOSSIER_LINK_REGEX.search(content)
                        existing_dossier = dossier_match.group(1) if dossier_match else None

                        # Look ahead for nested user notes and bullets
                        user_notes = []
                        indent_level = len(indent)
                        j = i + 1
                        while j < len(lines):
                            next_line = lines[j]
                            if not next_line.strip():
                                j += 1
                                continue
                            next_indent = len(next_line) - len(next_line.lstrip())
                            if next_indent <= indent_level:
                                break
                            
                            # Check if dossier link exists in sub-bullets
                            if not existing_dossier:
                                sub_dossier_match = DOSSIER_LINK_REGEX.search(next_line)
                                if sub_dossier_match:
                                    existing_dossier = sub_dossier_match.group(1)

                            # Record notes that aren't system AI updates
                            if not AI_UPDATE_BULLET_REGEX.match(next_line):
                                user_notes.append(next_line.strip())
                            j += 1

                        priority = 12 if any('focus' in t.lower() for t in tags) else 5
                        if user_notes:
                            priority += 2  # Boost priority if user left comments

                        tasks.append({
                            'id': f"{rel_file_path}:{line_number}",
                            'file': str(rel_file_path),
                            'abs_path': str(file_path),
                            'line_number': line_number,
                            'raw_line': line.rstrip('\r\n'),
                            'title': title_clean,
                            'tags': [t.lower() for t in tags],
                            'priority': priority,
                            'dossier': existing_dossier,
                            'user_notes': user_notes
                        })
                i += 1

    tasks.sort(key=lambda t: t['priority'], reverse=True)
    return tasks


def append_progress_to_note(vault_path: Path, rel_file: str, line_number: int,
                            tag_replacement: Optional[str],
                            summary_text: str,
                            dossier_rel_path: Optional[str] = None,
                            question_text: Optional[str] = None) -> bool:
    """Updates the task in place with inline dossier link and consolidated single Latest Update."""
    abs_path = vault_path / rel_file
    if not abs_path.exists():
        print(f"Error: File not found {abs_path}", file=sys.stderr)
        return False

    with open(abs_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    idx = line_number - 1
    if idx < 0 or idx >= len(lines):
        print(f"Error: Invalid line number {line_number}", file=sys.stderr)
        return False

    target_line = lines[idx]
    indent_match = re.match(r'^(\s*)', target_line)
    indent = indent_match.group(1) if indent_match else ''
    sub_indent = indent + "  "

    # Normalize line
    new_line = target_line.rstrip('\r\n')

    # Replace or update tag
    if tag_replacement:
        new_line = AI_TAG_REGEX.sub(tag_replacement, new_line, count=1)
        if tag_replacement == '#ai/done':
            new_line = re.sub(r'^- \[[ \t]\]', '- [x]', new_line)

    # Ensure inline dossier link is present (Option 1)
    if dossier_rel_path:
        dossier_clean = dossier_rel_path.strip()
        if not DOSSIER_LINK_REGEX.search(new_line):
            # Append inline link [[Dossier|📄]] right after the tag or at end of line
            dossier_tag = f"[[{dossier_clean}|📄]]"
            # If tag exists, place right after tag
            tag_match = AI_TAG_REGEX.search(new_line)
            if tag_match:
                end_pos = tag_match.end()
                new_line = new_line[:end_pos] + " " + dossier_tag + new_line[end_pos:]
            else:
                new_line = new_line + " " + dossier_tag

    lines[idx] = new_line + "\n"

    # Identify and remove previous AI updates or questions under this task to avoid clutter
    indent_level = len(indent)
    j = idx + 1
    lines_to_delete = []
    while j < len(lines):
        next_line = lines[j]
        if not next_line.strip():
            j += 1
            continue
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_indent <= indent_level:
            break
        if AI_UPDATE_BULLET_REGEX.match(next_line):
            lines_to_delete.append(j)
        j += 1

    for delete_idx in reversed(lines_to_delete):
        del lines[delete_idx]

    # Prepare fresh consolidated bullets
    today = datetime.now().strftime('%Y-%m-%d')
    new_bullets = []
    new_bullets.append(f"{sub_indent}- **Latest Update ({today})**: {summary_text}\n")
    if question_text:
        new_bullets.append(f"{sub_indent}- **Question for Peter**: {question_text}\n")

    # Insert bullets directly after task line
    insert_pos = idx + 1
    for bullet in reversed(new_bullets):
        lines.insert(insert_pos, bullet)

    with open(abs_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    return True


def main():
    parser = argparse.ArgumentParser(description="Obsidian AI Task Scanner & Living Dossier Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan subcommand
    scan_parser = subparsers.add_parser("scan", help="Scan vault for #ai tasks")
    scan_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    scan_parser.add_argument("--json", action="store_true", help="Output JSON format")
    scan_parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks returned")

    # update subcommand
    update_parser = subparsers.add_parser("update", help="Update task in original note")
    update_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    update_parser.add_argument("--file", required=True, help="Relative file path in vault")
    update_parser.add_argument("--line", required=True, type=int, help="Line number of the task")
    update_parser.add_argument("--new-tag", default=None, help="New tag (e.g., #ai/in-progress, #ai/done)")
    update_parser.add_argument("--summary", required=True, help="Consolidated latest status summary")
    update_parser.add_argument("--dossier", default=None, help="Relative path to living dossier (without dates in filename)")
    update_parser.add_argument("--question", default=None, help="Optional active question for Peter")

    args = parser.parse_args()
    vault_path = Path(args.vault).resolve()

    if args.command == "scan":
        tasks = scan_vault(vault_path)
        if args.limit > 0:
            tasks = tasks[:args.limit]
        if args.json:
            print(json.dumps(tasks, indent=2))
        else:
            if not tasks:
                print(f"No #ai tasks found in vault: {vault_path}")
            else:
                print(f"Found {len(tasks)} #ai task(s) in {vault_path}:\n")
                for t in tasks:
                    dossier_info = f" [Living Dossier: {t['dossier']}]" if t['dossier'] else " [New Task]"
                    print(f"[{t['priority']}] {t['file']}:{t['line_number']}{dossier_info}")
                    print(f"     Task: {t['title']} ({', '.join(t['tags'])})")
                    if t['user_notes']:
                        for n in t['user_notes']:
                            print(f"     Note: {n}")
                    print()

    elif args.command == "update":
        success = append_progress_to_note(
            vault_path=vault_path,
            rel_file=args.file,
            line_number=args.line,
            tag_replacement=args.new_tag,
            summary_text=args.summary,
            dossier_rel_path=args.dossier,
            question_text=args.question
        )
        if success:
            print(f"Successfully updated {args.file}:{args.line}")
            sys.exit(0)
        else:
            print(f"Failed to update {args.file}:{args.line}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
