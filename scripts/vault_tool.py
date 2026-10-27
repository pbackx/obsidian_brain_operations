#!/usr/bin/env python3
"""
vault_tool.py - Fast CLI utility for discovering, inspecting, updating, and archiving
1-file-per-task Markdown notes in an Obsidian Bases & Kanban workflow.

Features:
- Scans all Markdown files in the vault (excluding .obsidian, Brain_Operations, Templates, and TODO/Archive).
- Discovers tasks strictly by YAML frontmatter (`status: ai-queue`).
- Ranks queued tasks by priority (`high`/`focus` > follow-up tasks > `normal` > `low`) and modification time.
- Updates task notes in-place: updates YAML frontmatter (`status`, `updated`, `summary`, `question`)
  and refreshes the single top `[!question]` / `[!success]` Latest AI Update callout without duplicate history.
- Moves tasks marked `status: archived` into the central `TODO/Archive/` folder.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_EXCLUDES = {'.obsidian', '.git', '.trash', 'Brain_Operations', 'Templates', 'node_modules', '.agents'}
FRONTMATTER_REGEX = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z', re.DOTALL)
H1_REGEX = re.compile(r'^#\s+(.+)$', re.MULTILINE)
AI_CALLOUT_REGEX = re.compile(
    r'(?:^>[ \t]*\[!(?:question|success|info|note)\][ \t]*Latest AI Update[^\n]*\n(?:^>[^\n]*\n)*\n*)',
    re.MULTILINE,
)

STATUS_ALIASES = {
    '#ai/done': 'completed',
    '#ai/question': 'needs-clarification',
    '#ai': 'ai-queue',
}


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], List[str], str]:
    """
    Parses simple flat YAML frontmatter using only the Python standard library.
    Returns (dict_of_keys, raw_lines, markdown_body).
    """
    match = FRONTMATTER_REGEX.match(text)
    if not match:
        return None, [], text

    fm_block, body = match.group(1), match.group(2)
    fm_lines = fm_block.splitlines()
    data: Dict[str, str] = {}
    for line in fm_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if ':' in stripped:
            k, v = stripped.split(':', 1)
            key = k.strip()
            val = v.strip()
            if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                val = val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            data[key] = val
    return data, fm_lines, body


def yaml_quote(value: str) -> str:
    clean = value.replace('\r', ' ').replace('\n', ' ').strip()
    escaped = clean.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def build_frontmatter(fm_lines: List[str], updates: Dict[str, Optional[str]]) -> str:
    """
    Updates existing frontmatter lines in-place while preserving key order and custom fields.
    Keys mapped to None in `updates` are removed.
    """
    out_lines: List[str] = []
    seen_keys = set()

    for line in fm_lines:
        stripped = line.strip()
        if ':' in stripped and not stripped.startswith('#'):
            k = stripped.split(':', 1)[0].strip()
            if k in updates:
                seen_keys.add(k)
                new_val = updates[k]
                if new_val is not None:
                    out_lines.append(f"{k}: {new_val}")
                continue
        out_lines.append(line)

    for k, new_val in updates.items():
        if k not in seen_keys and new_val is not None:
            out_lines.append(f"{k}: {new_val}")

    return "---\n" + "\n".join(out_lines) + "\n---\n"


def compute_priority(priority_str: str, is_followup: bool) -> int:
    p = priority_str.lower().strip()
    if p in ('high', 'focus', 'urgent'):
        score = 12
    elif p == 'low':
        score = 3
    else:
        score = 5
    if is_followup:
        score += 2
    return score


def scan_vault(vault_path: Path, excludes: Optional[set] = None, target_status: str = "ai-queue") -> List[Dict[str, Any]]:
    if excludes is None:
        excludes = DEFAULT_EXCLUDES

    tasks: List[Dict[str, Any]] = []
    if not vault_path.exists() or not vault_path.is_dir():
        print(f"Error: Vault path does not exist: {vault_path}", file=sys.stderr)
        return tasks

    archive_dir = (vault_path / "TODO" / "Archive").resolve()

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in sorted(dirs) if d not in excludes and not d.startswith('.')]
        root_path = Path(root).resolve()
        if root_path == archive_dir or archive_dir in root_path.parents:
            continue

        rel_root = Path(root).relative_to(vault_path)
        if any(part in excludes for part in rel_root.parts):
            continue

        for file in sorted(files):
            if not file.endswith('.md'):
                continue

            file_path = Path(root) / file
            rel_file_path = file_path.relative_to(vault_path)

            try:
                content = file_path.read_text(encoding='utf-8', errors='replace')
            except Exception as e:
                print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
                continue

            fm, _, body = parse_frontmatter(content)
            if not fm:
                continue

            status = fm.get('status', '').lower().strip()
            if status != target_status.lower():
                continue

            h1_match = H1_REGEX.search(body)
            title = fm.get('title') or (h1_match.group(1).strip() if h1_match else file_path.stem)
            category = fm.get('category', 'general')
            priority_label = fm.get('priority', 'normal')
            is_followup = bool(fm.get('summary') or fm.get('question'))
            priority_score = compute_priority(priority_label, is_followup)
            mtime = file_path.stat().st_mtime

            tasks.append({
                'id': str(rel_file_path),
                'file': str(rel_file_path),
                'abs_path': str(file_path),
                'title': title,
                'status': status,
                'category': category,
                'priority_label': priority_label,
                'priority': priority_score,
                'created': fm.get('created', ''),
                'updated': fm.get('updated', ''),
                'summary': fm.get('summary', ''),
                'question': fm.get('question', ''),
                '_mtime': mtime,
            })

    tasks.sort(key=lambda t: (t['priority'], t['_mtime']), reverse=True)
    for t in tasks:
        t.pop('_mtime', None)
    return tasks


def update_task_note(
    vault_path: Path,
    rel_file: str,
    new_status: str,
    summary_text: str,
    question_text: Optional[str] = None,
) -> bool:
    """
    Updates a task note's YAML frontmatter and top Latest AI Update callout in-place.
    """
    abs_path = vault_path / rel_file
    if not abs_path.exists():
        print(f"Error: Task file not found: {abs_path}", file=sys.stderr)
        return False

    normalized_status = STATUS_ALIASES.get(new_status.lower().strip(), new_status.lower().strip())
    today = datetime.now().strftime('%Y-%m-%d')

    content = abs_path.read_text(encoding='utf-8', errors='replace')
    fm, fm_lines, body = parse_frontmatter(content)
    if fm is None:
        fm = {}
        fm_lines = []

    updates: Dict[str, Optional[str]] = {}
    if 'type' not in fm:
        updates['type'] = 'task'
    updates['status'] = normalized_status
    if 'created' not in fm:
        updates['created'] = today
    updates['updated'] = today
    updates['summary'] = yaml_quote(summary_text)

    if normalized_status == 'needs-clarification' and question_text:
        updates['question'] = yaml_quote(question_text)
    elif question_text:
        updates['question'] = yaml_quote(question_text)
    else:
        updates['question'] = None

    new_fm_str = build_frontmatter(fm_lines, updates)

    # Build the refreshed callout block
    if normalized_status == 'needs-clarification':
        callout = (
            f"> [!question] Latest AI Update ({today})\n"
            f"> **Summary:** {summary_text.strip()}  \n"
            f"> **Question for you:** {(question_text or 'Please review the findings below and add your remarks.').strip()}\n\n"
        )
    else:
        callout = (
            f"> [!success] Latest AI Update ({today})\n"
            f"> **Summary:** {summary_text.strip()}\n\n"
        )

    # Remove any previous Latest AI Update callout block
    cleaned_body = AI_CALLOUT_REGEX.sub('', body.lstrip('\r\n'))

    # Insert callout right after the H1 heading (or at the top of the body if no H1)
    h1_match = re.match(r'^(# [^\n]+\n+)', cleaned_body)
    if h1_match:
        h1_part = h1_match.group(1)
        rest_body = cleaned_body[len(h1_part):].lstrip('\r\n')
        new_body = f"{h1_part.rstrip()}\n\n{callout}{rest_body}"
    else:
        new_body = f"# {abs_path.stem}\n\n{callout}{cleaned_body}"

    abs_path.write_text(new_fm_str + "\n" + new_body.rstrip() + "\n", encoding='utf-8')
    return True


def archive_tasks(vault_path: Path, dry_run: bool = False) -> List[Dict[str, str]]:
    """
    Moves any task note outside `TODO/Archive` that has `status: archived` into `TODO/Archive/`.
    """
    archived_tasks = scan_vault(vault_path, target_status="archived")
    archive_dir = vault_path / "TODO" / "Archive"
    moved: List[Dict[str, str]] = []

    if not archived_tasks:
        return moved

    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for t in archived_tasks:
        src = Path(t['abs_path'])
        dst = archive_dir / src.name
        counter = 2
        while dst.exists() and dst.resolve() != src.resolve():
            dst = archive_dir / f"{src.stem} ({counter}){src.suffix}"
            counter += 1

        rel_dst = str(dst.relative_to(vault_path))
        if not dry_run:
            shutil.move(str(src), str(dst))
        moved.append({'from': t['file'], 'to': rel_dst})

    return moved


def main():
    parser = argparse.ArgumentParser(description="Obsidian Bases Task Scanner & In-Place Dossier Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan subcommand
    scan_parser = subparsers.add_parser("scan", help="Scan vault for status: ai-queue task notes")
    scan_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    scan_parser.add_argument("--json", action="store_true", help="Output JSON format")
    scan_parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks returned")
    scan_parser.add_argument("--status", default="ai-queue", help="Filter by status (default: ai-queue)")

    # next subcommand
    next_parser = subparsers.add_parser("next", help="Get next highest-priority status: ai-queue task")
    next_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    next_parser.add_argument("--exclude", default=None, help="Comma-separated relative file paths to exclude")

    # update subcommand
    update_parser = subparsers.add_parser("update", help="Update task frontmatter and callout in-place")
    update_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    update_parser.add_argument("--file", required=True, help="Relative file path in vault")
    update_parser.add_argument(
        "--status",
        default=None,
        help="New status: completed | needs-clarification (default: needs-clarification if --question else completed)",
    )
    update_parser.add_argument("--new-tag", default=None, help="Deprecated alias for --status")
    update_parser.add_argument("--summary", required=True, help="Consolidated 1-sentence status summary")
    update_parser.add_argument("--question", default=None, help="Optional active clarifying question for the user")

    # archive subcommand
    archive_parser = subparsers.add_parser("archive", help="Move status: archived task notes into TODO/Archive/")
    archive_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    archive_parser.add_argument("--dry-run", action="store_true", help="Preview files to move without moving")

    args = parser.parse_args()
    vault_path = Path(args.vault).resolve()

    if args.command == "scan":
        tasks = scan_vault(vault_path, target_status=args.status)
        if args.limit > 0:
            tasks = tasks[:args.limit]
        if args.json:
            print(json.dumps(tasks, indent=2))
        else:
            if not tasks:
                print(f"No '{args.status}' tasks found in vault: {vault_path}")
            else:
                print(f"Found {len(tasks)} '{args.status}' task(s) in {vault_path}:\n")
                for t in tasks:
                    print(f"[{t['priority']}] {t['file']} (category: {t['category']}, priority: {t['priority_label']})")
                    print(f"     Title: {t['title']}")
                    if t['summary']:
                        print(f"     Previous Summary: {t['summary']}")
                    print()

    elif args.command == "next":
        excluded = {x.strip() for x in args.exclude.split(',') if x.strip()} if args.exclude else set()
        tasks = scan_vault(vault_path, target_status="ai-queue")
        next_task = None
        for t in tasks:
            if t['id'] not in excluded and t['file'] not in excluded:
                next_task = t
                break
        if next_task:
            print(json.dumps(next_task, indent=2))
        else:
            print("null")

    elif args.command == "update":
        chosen_status = args.status or args.new_tag
        if not chosen_status:
            chosen_status = "needs-clarification" if args.question else "completed"
        success = update_task_note(
            vault_path=vault_path,
            rel_file=args.file,
            new_status=chosen_status,
            summary_text=args.summary,
            question_text=args.question,
        )
        if success:
            print(f"Successfully updated {args.file} (status={STATUS_ALIASES.get(chosen_status.lower(), chosen_status.lower())})")
            sys.exit(0)
        else:
            print(f"Failed to update {args.file}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "archive":
        moved = archive_tasks(vault_path, dry_run=args.dry_run)
        if not moved:
            print("No tasks with status: archived found outside TODO/Archive/.")
        else:
            prefix = "[DRY-RUN] " if args.dry_run else ""
            print(f"{prefix}Archived {len(moved)} task(s) to TODO/Archive/:")
            for m in moved:
                print(f"  - {m['from']} -> {m['to']}")


if __name__ == "__main__":
    main()
