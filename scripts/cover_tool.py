#!/usr/bin/env python3
"""
cover_tool.py - Local SVG Emoji Cover Banner Generator for Obsidian Bases & Kanban Boards.

Features:
- Generates crisp, lightweight (~1.5 KB) 480x120 (0.25 aspect ratio / 4:1) SVG banners
  combining 1-3 topic emojis with category-themed gradients and a frosted contrast capsule.
- Zero external dependencies (Python standard library only) and zero network calls.
- Automatically picks 3 contextually relevant emojis from a task note's title, category,
  summary, and remarks when `--emoji` is not explicitly provided.
- Backfills / ensures covers across all cards on the `🤖 AI Workflow Board` (including
  newly queued `status: ai-queue` cards) while preserving file modification times (`mtime`).
"""

import argparse
import html
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import vault_tool

COVER_SUBDIR = Path("TODO") / "files" / "covers"
SVG_WIDTH = 480
SVG_HEIGHT = 120

# Category gradient palettes aligned with Tasks.base category strip colors
CATEGORY_PALETTES: Dict[str, Tuple[str, str, str]] = {
    "home-automation": ("#0f766e", "#2a9d8f", "#14b8a6"),
    "lego-ai": ("#b45309", "#f4a261", "#f59e0b"),
    "games": ("#991b1b", "#e76f51", "#f97316"),
    "music": ("#5b21b6", "#9b5de5", "#c084fc"),
    "language": ("#9f1239", "#e63946", "#fb7185"),
    "freewrite": ("#334155", "#6c757d", "#94a3b8"),
    "general": ("#1e3a8a", "#457b9d", "#38bdf8"),
}

CATEGORY_DEFAULT_EMOJIS: Dict[str, List[str]] = {
    "home-automation": ["🏠", "⚡", "📡"],
    "lego-ai": ["🧱", "🤖", "⚙️"],
    "games": ["🎮", "🕹️", "🏆"],
    "music": ["🎵", "🎹", "🎧"],
    "language": ["🇯🇵", "🈶", "📚"],
    "freewrite": ["✍️", "📄", "💭"],
    "general": ["🧠", "🚀", "✨"],
}

# Ordered keyword rules: (regex_pattern, emoji)
KEYWORD_EMOJI_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(kanban|task\s*board|cards?\s*view)\b", re.I), "📋"),
    (re.compile(r"\b(cover\s*image|image\s*property|emoji|banner|icon)\b", re.I), "🎨"),
    (re.compile(r"\b(image\s*property|visual|polish|theme|style)\b", re.I), "✨"),
    (re.compile(r"\b(solis|sma|solar|pv|zonnepanelen)\b", re.I), "☀️"),
    (re.compile(r"\b(modbus|rs485|tcp|kabel|wiring|plug)\b", re.I), "🔌"),
    (re.compile(r"\b(victron|battery|batterij|ups)\b", re.I), "🔋"),
    (re.compile(r"\b(home\s*assistant|hass|smart\s*home|domotica)\b", re.I), "🏠"),
    (re.compile(r"\b(energy|electricity|power|tariff|eneco|stroom|watt|kwh)\b", re.I), "⚡"),
    (re.compile(r"\b(meerwaardebelasting|portfolio|stock|broker|ibkr|trading|market|invest\w*)\b", re.I), "📈"),
    (re.compile(r"\b(tax|meerwaardebelasting|belasting\w*|euro|price|cost|finance|bank)\b", re.I), "💶"),
    (re.compile(r"\b(calculat\w*|bereken\w*|formula|math)\b", re.I), "🧮"),
    (re.compile(r"\b(biohack\w*|dna|health|longevity|neuro\w*|oxygen|detox)\b", re.I), "🧬"),
    (re.compile(r"\b(recording\w*|video\w*|vimeo|film\w*|movie\w*|youtube|camera|gopro)\b", re.I), "🎬"),
    (re.compile(r"\b(jellyfin|tv\s*show|media\s*server|stream\w*|samsung\s*tv)\b", re.I), "📺"),
    (re.compile(r"\b(quantum|qubit|circuit)\b", re.I), "⚛️"),
    (re.compile(r"\b(kaggle|competition|contest|trophy|benchmark)\b", re.I), "🏆"),
    (re.compile(r"\b(mailbox|email|e-mail|imap|smtp|postbox|newsletter\w*)\b", re.I), "📬"),
    (re.compile(r"\b(calendar|kalender|caldav|schedule|agenda)\b", re.I), "📅"),
    (re.compile(r"\b(sync\w*|vdirsyncer|rclone|migration)\b", re.I), "🔄"),
    (re.compile(r"\b(clawdbot|nanoclaw|openclaw|nemoclaw|agent\w*|(?<!home\s)assistant|llm|ai|ollama|mistral|gemini|claude)\b", re.I), "🤖"),
    (re.compile(r"\b(brain|memory|cognitive|knowledge)\b", re.I), "🧠"),
    (re.compile(r"\b(obsidian|vault|markdown|note|notebook\w*|dossier)\b", re.I), "📓"),
    (re.compile(r"\b(proxmox|hypervisor|lxc|vm|server|homelab|cluster|kubernetes)\b", re.I), "🖥️"),
    (re.compile(r"\b(backup\w*|vzdump|pbs|synology|nas|storage|ssd|nvme|disk|archive)\b", re.I), "💾"),
    (re.compile(r"\b(pfsense|firewall|security|wireguard|vpn|failover|canary|auth\w*|yubikey|lastpass|strategy)\b", re.I), "🛡️"),
    (re.compile(r"\b(4g|lte|wan|cellular|antenna|zigbee|bluetooth|ble|wifi|mqtt|network)\b", re.I), "📡"),
    (re.compile(r"\b(spreadsheet\w*|onlyoffice|collabora|excel|sheets|csv|table)\b", re.I), "📊"),
    (re.compile(r"\b(document\w*|pdf|wopi|paper|book|read\w*|course\w*)\b", re.I), "📑"),
    (re.compile(r"\b(victoriametrics|grafana|telemetry|metrics|dashboard\w*|monitor\w*)\b", re.I), "📈"),
    (re.compile(r"\b(lego|brick\w*|technic|rebrickable|moc)\b", re.I), "🧱"),
    (re.compile(r"\b(turntable|motor\w*|gear\w*|calibrat\w*|robot\w*|sortbot)\b", re.I), "⚙️"),
    (re.compile(r"\b(game\w*|gaming|steam\s*deck|fortnite|minecraft|skyrim|portal|zelda|launchbox)\b", re.I), "🎮"),
    (re.compile(r"\b(music|bitwig|maschine|roli|audio|synth|sound|microphone)\b", re.I), "🎵"),
    (re.compile(r"\b(japan\w*|lingq|anki|manga|kanji|ime)\b", re.I), "🇯🇵"),
    (re.compile(r"\b(freewrite|typewriter|writing|draft\w*)\b", re.I), "✍️"),
    (re.compile(r"\b(keyboard\w*|voyager|kyria|wooting|qmk|keycap\w*)\b", re.I), "⌨️"),
    (re.compile(r"\b(3d\s*print\w*|prusa|thingiverse|um3)\b", re.I), "🖨️"),
    (re.compile(r"\b(code|compil\w*|script\w*|python|linux|terminal|cli|ssh)\b", re.I), "💻"),
]

# Characters that frequently default to text presentation unless U+FE0F is present
NEEDS_VS16 = {
    0x2600, 0x2601, 0x260E, 0x261D, 0x2620, 0x2622, 0x2623, 0x2626, 0x262A,
    0x262E, 0x262F, 0x2638, 0x2639, 0x263A, 0x2640, 0x2642, 0x2660, 0x2663,
    0x2665, 0x2666, 0x2668, 0x267B, 0x267E, 0x2692, 0x2694, 0x2695, 0x2696,
    0x2697, 0x2699, 0x269B, 0x269C, 0x26A0, 0x26A1, 0x26B0, 0x26B1, 0x26BD,
    0x26BE, 0x26C4, 0x26C5, 0x26C8, 0x26CE, 0x26CF, 0x26D1, 0x26D3, 0x26D4,
    0x26E9, 0x26EA, 0x26F0, 0x26F1, 0x26F2, 0x26F3, 0x26F4, 0x26F5, 0x26F7,
    0x26F8, 0x26F9, 0x26FA, 0x26FD, 0x2702, 0x2708, 0x2709, 0x270C, 0x270D,
    0x270F, 0x2712, 0x2714, 0x2716, 0x271D, 0x2721, 0x2728, 0x2733, 0x2734,
    0x2744, 0x2747, 0x2757, 0x2763, 0x2764, 0x27A1, 0x2934, 0x2935, 0x2B05,
    0x2B06, 0x2B07, 0x3030, 0x303D, 0x3297, 0x3299,
    0x1F321, 0x1F324, 0x1F325, 0x1F326, 0x1F327, 0x1F328, 0x1F329, 0x1F32A,
    0x1F32B, 0x1F32C, 0x1F336, 0x1F37D, 0x1F396, 0x1F397, 0x1F399, 0x1F39A,
    0x1F39B, 0x1F39E, 0x1F39F, 0x1F3CB, 0x1F3CC, 0x1F3CD, 0x1F3CE, 0x1F3D4,
    0x1F3D5, 0x1F3D6, 0x1F3D7, 0x1F3D8, 0x1F3D9, 0x1F3DA, 0x1F3DB, 0x1F3DC,
    0x1F3DD, 0x1F3DE, 0x1F3DF, 0x1F3F3, 0x1F3F5, 0x1F3F7, 0x1F43F, 0x1F441,
    0x1F4FD, 0x1F549, 0x1F54A, 0x1F56F, 0x1F570, 0x1F573, 0x1F574, 0x1F575,
    0x1F576, 0x1F577, 0x1F578, 0x1F579, 0x1F587, 0x1F58A, 0x1F58B, 0x1F58C,
    0x1F58D, 0x1F590, 0x1F5A5, 0x1F5A8, 0x1F5B1, 0x1F5B2, 0x1F5BC, 0x1F5C2,
    0x1F5C3, 0x1F5C4, 0x1F5D1, 0x1F5D2, 0x1F5D3, 0x1F5DC, 0x1F5DD, 0x1F5DE,
    0x1F5E1, 0x1F5E3, 0x1F5E8, 0x1F5EF, 0x1F5F3, 0x1F5FA, 0x1F6CB, 0x1F6CD,
    0x1F6CE, 0x1F6CF, 0x1F6E0, 0x1F6E1, 0x1F6E2, 0x1F6E3, 0x1F6E4, 0x1F6E5,
    0x1F6E9, 0x1F6F0, 0x1F6F3,
}


def slugify(name: str) -> str:
    """Converts a filename stem or title into a clean lowercase ASCII slug."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug or "task-cover"


def normalize_emoji_cluster(cluster: str) -> str:
    """Ensures single-codepoint dual-presentation emojis include U+FE0F for color rendering."""
    cluster = cluster.strip()
    if not cluster:
        return cluster
    if len(cluster) == 1 and ord(cluster[0]) in NEEDS_VS16:
        return cluster + "\ufe0f"
    return cluster


def split_emoji_clusters(raw: str) -> List[str]:
    """
    Splits an emoji string (either space/comma-separated or contiguous) into up to 3
    individual emoji grapheme clusters, preserving ZWJ sequences, regional indicator pairs,
    skin tone modifiers, and variation selectors.
    """
    cleaned = raw.strip()
    if not cleaned:
        return []

    if re.search(r"[\s,]+", cleaned):
        tokens = [t.strip() for t in re.split(r"[\s,]+", cleaned) if t.strip()]
        return [normalize_emoji_cluster(t) for t in tokens[:3]]

    clusters: List[str] = []
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch.isspace() or ch == ",":
            i += 1
            continue

        # Regional indicator pair (flags like 🇯🇵)
        if 0x1F1E6 <= ord(ch) <= 0x1F1FF and i + 1 < n and 0x1F1E6 <= ord(cleaned[i + 1]) <= 0x1F1FF:
            clusters.append(cleaned[i : i + 2])
            i += 2
            continue

        cluster = ch
        i += 1
        while i < n:
            nxt = cleaned[i]
            cp = ord(nxt)
            # Variation selectors, skin tone modifiers, combining enclosing keycap
            if cp in (0xFE0E, 0xFE0F, 0x20E3) or (0x1F3FB <= cp <= 0x1F3FF):
                cluster += nxt
                i += 1
            # Zero Width Joiner sequence
            elif cp == 0x200D and i + 1 < n:
                cluster += nxt + cleaned[i + 1]
                i += 2
            else:
                break
        clusters.append(normalize_emoji_cluster(cluster))

    return clusters[:3]


def pick_emojis(
    title: str,
    category: str = "general",
    summary: str = "",
    remarks: str = "",
    count: int = 3,
) -> List[str]:
    """
    Selects up to `count` distinct, contextually fitting emojis based on the task's
    title (weighted highest), summary/remarks, and category fallback palette.
    """
    cat_key = (category or "general").strip().lower()
    chosen: List[str] = []

    def add_emoji(e: str) -> None:
        norm = normalize_emoji_cluster(e)
        if norm and norm not in chosen and len(chosen) < count:
            chosen.append(norm)

    # Pass 1: Match against task title (highest relevance)
    for pattern, emoji in KEYWORD_EMOJI_RULES:
        if pattern.search(title or ""):
            add_emoji(emoji)
            if len(chosen) >= count:
                return chosen

    # Pass 2: Match against summary and user remarks
    secondary_text = f"{summary or ''}\n{remarks or ''}"
    for pattern, emoji in KEYWORD_EMOJI_RULES:
        if pattern.search(secondary_text):
            add_emoji(emoji)
            if len(chosen) >= count:
                return chosen

    # Pass 3: Fill remaining slots from category defaults
    defaults = CATEGORY_DEFAULT_EMOJIS.get(cat_key, CATEGORY_DEFAULT_EMOJIS["general"])
    for emoji in defaults:
        add_emoji(emoji)
        if len(chosen) >= count:
            return chosen

    return chosen


def render_emoji_banner_svg(emojis: List[str], category: str = "general") -> str:
    """
    Renders a 480x120 (0.25 aspect ratio / 4:1) SVG banner featuring a category gradient,
    subtle ambient geometry, a frosted dark-glass capsule for contrast, and 1-3 centered emojis.
    """
    cat_key = (category or "general").strip().lower()
    c1, c2, c3 = CATEGORY_PALETTES.get(cat_key, CATEGORY_PALETTES["general"])

    normalized = [normalize_emoji_cluster(e) for e in emojis if e.strip()][:3]
    if not normalized:
        normalized = [normalize_emoji_cluster(e) for e in CATEGORY_DEFAULT_EMOJIS.get(cat_key, CATEGORY_DEFAULT_EMOJIS["general"])]

    num = len(normalized)
    if num == 1:
        pill_x, pill_w = 184, 112
        positions = [240]
        font_size = 58
    elif num == 2:
        pill_x, pill_w = 135, 210
        positions = [192, 288]
        font_size = 54
    else:
        pill_x, pill_w = 86, 308
        positions = [148, 240, 332]
        font_size = 52

    text_elements = []
    for x_pos, emoji in zip(positions, normalized):
        escaped = html.escape(emoji)
        text_elements.append(
            f'  <text x="{x_pos}" y="60" font-family="\'Segoe UI Emoji\', \'Apple Color Emoji\', \'Noto Color Emoji\', \'Twemoji Mozilla\', sans-serif" '
            f'font-size="{font_size}" text-anchor="middle" dominant-baseline="central">{escaped}</text>'
        )

    texts_block = "\n".join(text_elements)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" width="{SVG_WIDTH}" height="{SVG_HEIGHT}">\n'
        f'  <defs>\n'
        f'    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">\n'
        f'      <stop offset="0%" stop-color="{c1}"/>\n'
        f'      <stop offset="52%" stop-color="{c2}"/>\n'
        f'      <stop offset="100%" stop-color="{c3}"/>\n'
        f'    </linearGradient>\n'
        f'  </defs>\n'
        f'  <rect width="{SVG_WIDTH}" height="{SVG_HEIGHT}" fill="url(#bg)"/>\n'
        f'  <circle cx="42" cy="18" r="88" fill="#ffffff" fill-opacity="0.07"/>\n'
        f'  <circle cx="442" cy="106" r="96" fill="#ffffff" fill-opacity="0.07"/>\n'
        f'  <circle cx="240" cy="-24" r="110" fill="#ffffff" fill-opacity="0.05"/>\n'
        f'  <rect x="{pill_x}" y="14" width="{pill_w}" height="92" rx="46" fill="#090d16" fill-opacity="0.24" stroke="#ffffff" stroke-opacity="0.30" stroke-width="1.5"/>\n'
        f'{texts_block}\n'
        f'</svg>\n'
    )


def generate_cover_file(
    vault_path: Path,
    slug: str,
    emojis: List[str],
    category: str = "general",
) -> str:
    """
    Writes `<VAULT_PATH>/TODO/files/covers/<slug>.svg` and returns the vault-relative path
    `TODO/files/covers/<slug>.svg`.
    """
    covers_dir = vault_path / COVER_SUBDIR
    covers_dir.mkdir(parents=True, exist_ok=True)
    rel_svg = COVER_SUBDIR / f"{slug}.svg"
    abs_svg = vault_path / rel_svg
    svg_content = render_emoji_banner_svg(emojis, category=category)
    abs_svg.write_text(svg_content, encoding="utf-8")
    return str(rel_svg).replace("\\", "/")


def generate_cover_for_note(
    vault_path: Path,
    rel_file: str,
    emoji_override: Optional[str] = None,
    update_frontmatter: bool = True,
    preserve_mtime: bool = False,
) -> Tuple[str, List[str]]:
    """
    Generates an emoji SVG banner for a task note and optionally updates its `cover`
    frontmatter property in-place. Returns `(wikilink_str, emojis_list)`.
    """
    abs_path = vault_path / rel_file
    if not abs_path.exists():
        raise FileNotFoundError(f"Task note not found: {abs_path}")

    orig_stat = abs_path.stat()
    content = abs_path.read_text(encoding="utf-8", errors="replace")
    fm, fm_lines, body = vault_tool.parse_frontmatter(content)
    if fm is None:
        fm = {}
        fm_lines = []

    h1_match = vault_tool.H1_REGEX.search(body)
    title = fm.get("title") or (h1_match.group(1).strip() if h1_match else abs_path.stem)
    category = fm.get("category", "general")
    summary = fm.get("summary", "")
    remarks = vault_tool.extract_user_remarks(body)

    if emoji_override:
        emojis = split_emoji_clusters(emoji_override)
    else:
        emojis = pick_emojis(title=title, category=category, summary=summary, remarks=remarks)

    slug = slugify(abs_path.stem)
    rel_svg = generate_cover_file(vault_path, slug=slug, emojis=emojis, category=category)
    wikilink = f"[[{rel_svg}]]"

    if update_frontmatter:
        updates: Dict[str, Optional[str]] = {
            "cover": vault_tool.yaml_quote(wikilink),
            "cover_credit": None,
        }
        new_fm = vault_tool.build_frontmatter(fm_lines, updates)
        abs_path.write_text(new_fm + "\n" + body.lstrip("\r\n").rstrip() + "\n", encoding="utf-8")
        if preserve_mtime:
            os.utime(abs_path, (orig_stat.st_atime, orig_stat.st_mtime))

    return wikilink, emojis


def is_ai_workflow_board_card(fm: Dict[str, str]) -> bool:
    """
    Matches the exact filter of `🤖 AI Workflow Board` in `Tasks.base`:
    - `type == "task"`
    - `!file.inFolder("TODO/Archive")`
    - `status == "ai-queue" or status == "needs-clarification" or summary.isTruthy() or status == "archived"`
    """
    if fm.get("type", "").strip().lower() != "task":
        return False
    status = fm.get("status", "").strip().lower()
    summary = fm.get("summary", "").strip()
    return status in ("ai-queue", "needs-clarification", "archived") or bool(summary)


def has_local_svg_cover(vault_path: Path, fm: Dict[str, str]) -> bool:
    """Returns True if the note already has a `cover` pointing to an existing local `.svg` file."""
    cover_val = fm.get("cover", "").strip()
    if not (cover_val.startswith("[[") and cover_val.endswith("]]")):
        return False
    inner = cover_val[2:-2].strip()
    if not inner.lower().endswith(".svg"):
        return False
    target = vault_path / inner
    return target.exists()


def ensure_board_covers(
    vault_path: Path,
    force: bool = False,
    only_status: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Scans the vault and generates/assigns emoji SVG covers for all cards on the
    `🤖 AI Workflow Board` (or filtered by `only_status`) that do not yet have a local
    `.svg` cover (or all matching cards if `force=True`).
    Preserves `mtime` so queue ordering is undisturbed.
    """
    results: List[Dict[str, str]] = []
    archive_dir = (vault_path / "TODO" / "Archive").resolve()

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in sorted(dirs) if d not in vault_tool.DEFAULT_EXCLUDES and not d.startswith(".")]
        root_path = Path(root).resolve()
        if root_path == archive_dir or archive_dir in root_path.parents:
            continue

        rel_root = Path(root).relative_to(vault_path)
        if any(part in vault_tool.DEFAULT_EXCLUDES for part in rel_root.parts):
            continue

        for file in sorted(files):
            if not file.endswith(".md"):
                continue

            file_path = Path(root) / file
            rel_file = str(file_path.relative_to(vault_path))
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            fm, _, _ = vault_tool.parse_frontmatter(content)
            if not fm:
                continue

            if only_status:
                if fm.get("status", "").strip().lower() != only_status.strip().lower():
                    continue
            else:
                if not is_ai_workflow_board_card(fm):
                    continue

            if not force and has_local_svg_cover(vault_path, fm):
                continue

            wikilink, emojis = generate_cover_for_note(
                vault_path=vault_path,
                rel_file=rel_file,
                update_frontmatter=True,
                preserve_mtime=True,
            )
            results.append({
                "file": rel_file,
                "status": fm.get("status", ""),
                "category": fm.get("category", "general"),
                "emojis": " ".join(emojis),
                "cover": wikilink,
            })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Local SVG Emoji Cover Generator for Obsidian Kanban Cards")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # generate subcommand (single note)
    gen_parser = subparsers.add_parser("generate", help="Generate and set an emoji SVG cover for a single task note")
    gen_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    gen_parser.add_argument("--file", required=True, help="Relative file path in vault")
    gen_parser.add_argument("--emoji", default=None, help="1-3 emojis (e.g. '☀️ 🔌 🔋'); auto-picked if omitted")
    gen_parser.add_argument("--preserve-mtime", action="store_true", help="Preserve note file mtime")

    # ensure / backfill subcommand (board-wide or status-filtered)
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="Ensure all cards on the AI Workflow Board (including ai-queue) have local emoji SVG covers",
    )
    ensure_parser.add_argument("--vault", required=True, help="Path to Obsidian vault")
    ensure_parser.add_argument("--force", action="store_true", help="Regenerate covers even if already present")
    ensure_parser.add_argument("--status", default=None, help="Optional status filter (e.g. ai-queue)")

    args = parser.parse_args()
    vault_path = Path(args.vault).resolve()

    if args.command == "generate":
        wikilink, emojis = generate_cover_for_note(
            vault_path=vault_path,
            rel_file=args.file,
            emoji_override=args.emoji,
            update_frontmatter=True,
            preserve_mtime=args.preserve_mtime,
        )
        print(f"Generated cover for {args.file}: {' '.join(emojis)} -> {wikilink}")

    elif args.command == "ensure":
        updated = ensure_board_covers(
            vault_path=vault_path,
            force=args.force,
            only_status=args.status,
        )
        if not updated:
            print("All target AI Workflow Board cards already have local SVG emoji covers.")
        else:
            print(f"Generated local SVG emoji covers for {len(updated)} card(s):")
            for item in updated:
                print(f"  - {item['file']} ({item['status']} / {item['category']}): {item['emojis']} -> {item['cover']}")


if __name__ == "__main__":
    main()
