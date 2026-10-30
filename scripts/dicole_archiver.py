#!/usr/bin/env python3
"""
Dicole.AI / Biohacker Summit Archiver & Jellyfin Integration Pipeline
====================================================================
Extracts Biohacker Summit recordings, pre-generated AI summaries, key takeaways,
habits, quotes, and transcripts from Dicole.AI, organises them as a structured
TV series for Jellyfin with rich Kodi/Jellyfin XML (.nfo) metadata, and exports
interlinked research notes into an Obsidian vault.

Author: Antigravity Autonomous Nightly Operator
License: MIT
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import shutil

API_DATABASE_URL = "https://dicole.ai/api/database"
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
FFMPEG_BIN = Path.home() / ".local" / "bin" / "ffmpeg"


def sanitize_filename(name: str) -> str:
    """Sanitize string for filesystem paths across Linux/Windows/NFS."""
    if not name:
        return "Untitled"
    # Replace invalid path characters
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    # Normalize whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "Untitled"


def query_dicole_db(table: str, columns: str = "*", filters: list = None, limit: int = None, public: bool = True) -> list:
    """Query Dicole.AI internal PostgREST database proxy."""
    payload = {
        "op": "from",
        "method": "select",
        "table": table,
        "columns": columns,
        "filters": filters or [],
        "public": public
    }
    if limit is not None:
        payload["limit"] = limit

    req = urllib.request.Request(
        API_DATABASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data") or []
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        print(f"[ERROR] HTTP {e.code} querying table '{table}': {err_msg}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[ERROR] Exception querying table '{table}': {e}", file=sys.stderr)
        return []


def get_all_summits() -> list:
    """Fetch all events matching 'summit' or 'biohack'."""
    events = query_dicole_db("events", columns="*")
    summits = []
    for ev in events:
        title = ev.get("title") or ""
        if "summit" in title.lower() or "biohack" in title.lower():
            summits.append(ev)
    summits.sort(key=lambda x: x.get("start_date") or x.get("created_at") or "", reverse=True)
    return summits


def extract_vimeo_url(embed_code: str, presentation_url: str) -> str:
    """Extract player.vimeo.com or vimeo.com URL from embed code or presentation url."""
    if embed_code:
        m = re.search(r'src=["\'](https?://player\.vimeo\.com/video/\d+[^"\']*)["\']', embed_code)
        if m:
            return m.group(1).split("?")[0]  # Strip parameters
        m = re.search(r'vimeo\.com/(\d+)', embed_code)
        if m:
            return f"https://player.vimeo.com/video/{m.group(1)}"

    if presentation_url:
        m = re.search(r'vimeo\.com/(?:video/)?(\d+)', presentation_url)
        if m:
            return f"https://player.vimeo.com/video/{m.group(1)}"

    return ""


def build_jellyfin_tvshow_nfo() -> str:
    """Generate top-level tvshow.nfo for Jellyfin."""
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Biohacker Summit</title>
  <originaltitle>Biohacker Summit</originaltitle>
  <showtitle>Biohacker Summit</showtitle>
  <plot>Recordings, keynote lectures, and masterclasses from the international Biohacker Summits focusing on human performance optimization, preventative health, longevity, sleep architecture, and circadian biology.</plot>
  <genre>Health &amp; Longevity</genre>
  <genre>Biohacking</genre>
  <genre>Science &amp; Technology</genre>
  <studio>Biohacker Center</studio>
</tvshow>
"""


def build_jellyfin_season_nfo(season_year: int, season_title: str, event_desc: str = "") -> str:
    """Generate season.nfo for Jellyfin."""
    desc = event_desc or f"Recordings and lectures from {season_title}."
    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<season>
  <title>{xml_escape(season_title)}</title>
  <seasonnumber>{season_year}</seasonnumber>
  <plot>{xml_escape(desc)}</plot>
</season>
"""


def format_plot_text(ai_data: dict, description: str = "") -> str:
    """Format the Jellyfin episode plot with a concise, readable AI summary."""
    summary = (ai_data.get("ai_summary") or "").strip()
    one_liner = (ai_data.get("ai_one_liner") or "").strip()

    lines = []
    if one_liner:
        lines.append(one_liner)

    if summary:
        bullet_lines = [l.strip() for l in summary.splitlines() if l.strip()]
        formatted_bullets = "\n".join(
            f"• {l}" if not l.startswith("•") and not l.startswith("-") else l
            for l in bullet_lines
        )
        lines.append(formatted_bullets)
    elif description:
        lines.append(description.strip())

    return "\n\n".join(lines).strip()


def build_jellyfin_episode_nfo(
    title: str,
    show_title: str,
    season_year: int,
    episode_num: int,
    speaker: str,
    premiered: str,
    ai_data: dict,
    description: str = ""
) -> str:
    """Generate episode .nfo for Jellyfin / Kodi."""
    plot = format_plot_text(ai_data, description)
    outline = ai_data.get("ai_one_liner") or description[:200]
    date_str = premiered.split("T")[0] if premiered else ""

    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>{xml_escape(title)}</title>
  <showtitle>{xml_escape(show_title)}</showtitle>
  <season>{season_year}</season>
  <episode>{episode_num}</episode>
  <outline>{xml_escape(outline)}</outline>
  <plot><![CDATA[{plot}]]></plot>
  <director>{xml_escape(speaker)}</director>
  <credits>{xml_escape(speaker)}</credits>
  <premiered>{date_str}</premiered>
  <genre>Biohacking</genre>
  <genre>Health &amp; Longevity</genre>
  <studio>Biohacker Center</studio>
</episodedetails>
"""


def build_obsidian_note(
    title: str,
    speaker: str,
    event_title: str,
    season_year: int,
    episode_num: int,
    premiered: str,
    vimeo_url: str,
    presentation_url: str,
    ai_data: dict,
    description: str = "",
    media_rel_path: str = ""
) -> str:
    """Generate a rich, interlinked Markdown note for the Obsidian vault."""
    date_str = premiered.split("T")[0] if premiered else ""
    one_liner = ai_data.get("ai_one_liner") or ""
    summary = ai_data.get("ai_summary") or description
    facts = ai_data.get("ai_facts") or ""
    habits = ai_data.get("ai_habits") or ""
    quotes = ai_data.get("ai_quotes") or ai_data.get("ai_top_quote") or ""
    transcript = ai_data.get("ai_edited_transcript") or ai_data.get("transcript") or ""

    tags = ["biohacking", "longevity", "summit", f"year/{season_year}"]
    if speaker:
        speaker_tag = re.sub(r'[^a-zA-Z0-9_]', "", speaker.lower().replace(" ", "_"))
        if speaker_tag:
            tags.append(f"speaker/{speaker_tag}")

    yaml_header = f"""---
type: resource
category: health
event: "{event_title}"
season: {season_year}
episode: {episode_num}
speaker: "{speaker}"
date: {date_str}
vimeo_url: "{vimeo_url}"
tags: [{', '.join(tags)}]
---

# S{season_year}E{episode_num:02d} - {speaker} - {title}

> [!abstract] {one_liner if one_liner else title}
> **Speaker:** {speaker}  
> **Event:** [[Fitness and Sports/Biohacker Summits/{event_title}|{event_title}]]  
> **Date:** {date_str}  
"""

    if media_rel_path:
        yaml_header += f"> **Media Library:** `{media_rel_path}`  \n"

    body = f"\n## Summary\n{summary}\n" if summary else ""

    if facts:
        body += f"\n## Key Takeaways & Facts\n{facts}\n"

    if habits:
        body += f"\n## Recommended Protocols & Habits\n{habits}\n"

    if quotes:
        body += f"\n## Notable Quotes\n> {quotes}\n"

    links = []
    if vimeo_url:
        links.append(f"- [Vimeo Video Stream]({vimeo_url})")
    if presentation_url:
        links.append(f"- [Presentation / Audio Asset]({presentation_url})")
    if links:
        body += "\n## Source Links\n" + "\n".join(links) + "\n"

    if transcript:
        body += f"""
---

## Full Transcript

<details>
<summary>Click to expand complete speech transcript</summary>

{transcript}

</details>
"""

    return yaml_header + body


def fetch_vimeo_thumbnail(vimeo_url: str, output_path: Path) -> bool:
    """Fetch the high-resolution official title slide thumbnail via Vimeo oEmbed."""
    if output_path.exists() and output_path.stat().st_size > 10_000:
        return True
    m = re.search(r'/video/(\d+)', vimeo_url)
    if not m:
        return False
    vid = m.group(1)
    oembed_url = f"https://vimeo.com/api/oembed.json?url=https://player.vimeo.com/video/{vid}"
    try:
        req = urllib.request.Request(oembed_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            raw_thumb = data.get("thumbnail_url", "")
            if not raw_thumb:
                return False
            high_thumb = raw_thumb.split("_")[0] + "_1280.jpg"
            req_img = urllib.request.Request(high_thumb, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req_img, timeout=10) as ir, open(output_path, "wb") as f:
                f.write(ir.read())
            return True
    except Exception as e:
        try:
            if "raw_thumb" in locals() and raw_thumb:
                req_fallback = urllib.request.Request(raw_thumb, headers={"User-Agent": DEFAULT_USER_AGENT})
                with urllib.request.urlopen(req_fallback, timeout=10) as ir, open(output_path, "wb") as f:
                    f.write(ir.read())
                return True
        except Exception:
            pass
        print(f"[WARN] Could not fetch thumbnail for {vimeo_url}: {e}", file=sys.stderr)
        return False


def setup_summit_artwork(event: dict, show_dir: Path, season_dir: Path):
    """Generate show & season level posters and fanart backgrounds."""
    logo_url = event.get("event_logo")
    if not logo_url:
        return

    cache_file = Path("/tmp") / f"event_logo_{event.get('id', 'default')}.jpg"
    if not cache_file.exists():
        try:
            req = urllib.request.Request(logo_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp, open(cache_file, "wb") as f:
                f.write(resp.read())
        except Exception as e:
            print(f"[WARN] Failed downloading event logo: {e}", file=sys.stderr)
            return

    if not cache_file.exists() or not FFMPEG_BIN.exists():
        return

    show_poster = show_dir / "poster.jpg"
    show_fanart = show_dir / "fanart.jpg"

    # 1. Show Poster (1000x1500, 2:3 vertical crop centered on flask logo)
    if not show_poster.exists():
        cmd = [
            str(FFMPEG_BIN), "-y", "-i", str(cache_file),
            "-vf", "crop=ih*2/3:ih:(iw-ih*2/3)/2:0,scale=1000:1500:flags=lanczos",
            str(show_poster)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if show_poster.exists():
            shutil.copyfile(show_poster, show_dir / "folder.jpg")

    # 2. Show Fanart / Backdrop (1920x1080)
    if not show_fanart.exists():
        cmd = [
            str(FFMPEG_BIN), "-y", "-i", str(cache_file),
            "-vf", "scale=1920:1080:flags=lanczos",
            str(show_fanart)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if show_fanart.exists():
            shutil.copyfile(show_fanart, show_dir / "backdrop.jpg")

    # 3. Season Poster & Fanart
    season_poster = season_dir / "poster.jpg"
    season_fanart = season_dir / "fanart.jpg"

    if show_poster.exists() and not season_poster.exists():
        shutil.copyfile(show_poster, season_poster)
        shutil.copyfile(show_poster, season_dir / "season-poster.jpg")
        shutil.copyfile(show_poster, season_dir / "folder.jpg")

    if show_fanart.exists() and not season_fanart.exists():
        shutil.copyfile(show_fanart, season_fanart)
        shutil.copyfile(show_fanart, season_dir / "backdrop.jpg")


def download_video_yt_dlp(vimeo_url: str, output_path: Path, quality: str = None) -> bool:
    """Download video using yt-dlp with Vimeo referrer and high quality muxing."""
    if output_path.exists() and output_path.stat().st_size > 5_000_000:
        print(f"[SKIP] Video already exists: {output_path.name} ({output_path.stat().st_size / (1024*1024):.1f} MB)")
        return True

    temp_path = output_path.with_suffix(".tmp.mp4")
    if temp_path.exists():
        temp_path.unlink()

    quality_fmt = quality or "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

    cmd = [
        "yt-dlp",
        "--referer", "https://dicole.ai/",
        "-f", quality_fmt,
        "--merge-output-format", "mp4",
        "-N", "8",
        "--retries", "10",
        "--fragment-retries", "10",
        "--no-mtime",
        "--no-playlist",
        "-o", str(temp_path),
        vimeo_url
    ]

    print(f"[DOWNLOADING] {vimeo_url} -> {output_path.name}...")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        if temp_path.exists() and temp_path.stat().st_size > 1_000_000:
            temp_path.rename(output_path)
            print(f"[SUCCESS] Downloaded: {output_path.name} ({output_path.stat().st_size / (1024*1024):.1f} MB)")
            return True
        else:
            print(f"[ERROR] Output file too small or missing after download: {temp_path}", file=sys.stderr)
            return False
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] yt-dlp failed for {vimeo_url}:\n{e.stdout[-500:]}", file=sys.stderr)
        if temp_path.exists():
            temp_path.unlink()
        return False
    except FileNotFoundError:
        print("[ERROR] yt-dlp binary not found in PATH!", file=sys.stderr)
        return False


def process_summit(
    event: dict,
    media_dir: Path,
    vault_dir: Path,
    download_videos: bool = True,
    max_items: int = None,
    quality: str = None,
    dry_run: bool = False
):
    """Process all talks for an individual Biohacker Summit."""
    event_id = event["id"]
    event_title = event.get("title") or "Biohacker Summit"
    event_date = event.get("start_date") or event.get("created_at") or ""

    # Parse year and location from title or date
    year_match = re.search(r'\b(20\d\d)\b', event_title)
    if year_match:
        season_year = int(year_match.group(1))
    elif event_date:
        season_year = int(event_date[:4])
    else:
        season_year = 2024

    # City extraction
    city_match = re.search(r'\b(Helsinki|Amsterdam|London|Stockholm|Tallinn|Toronto|Tokyo)\b', event_title, re.IGNORECASE)
    city = city_match.group(1).capitalize() if city_match else "International"
    season_name = f"Season {season_year} - {city}"

    print(f"\n========================================================")
    print(f"Processing Event: {event_title}")
    print(f"Season: {season_year} ({season_name}) | Event ID: {event_id}")
    print(f"========================================================")

    # Prepare directories
    show_dir = media_dir / "Biohacker Summit"
    season_dir = show_dir / season_name
    obsidian_summit_dir = vault_dir / "Fitness and Sports" / "Biohacker Summits" / f"{season_year} {city}"

    if not dry_run:
        season_dir.mkdir(parents=True, exist_ok=True)
        obsidian_summit_dir.mkdir(parents=True, exist_ok=True)

        # Write top-level tvshow.nfo
        tvshow_nfo_path = show_dir / "tvshow.nfo"
        if not tvshow_nfo_path.exists():
            tvshow_nfo_path.write_text(build_jellyfin_tvshow_nfo(), encoding="utf-8")
            print(f"[CREATED] {tvshow_nfo_path}")

        # Write season.nfo
        season_nfo_path = season_dir / "season.nfo"
        if not season_nfo_path.exists():
            season_nfo_path.write_text(build_jellyfin_season_nfo(season_year, season_name, event.get("description", "")), encoding="utf-8")
            print(f"[CREATED] {season_nfo_path}")

        # Setup show & season artwork (posters, backdrops)
        setup_summit_artwork(event, show_dir, season_dir)

    # Fetch program items
    items = query_dicole_db("program_items", columns="*", filters=[["eq", "event_id", event_id]])
    print(f"Discovered {len(items)} talks in event database.")

    # Sort items by presentation_date or sort_order or title
    items.sort(key=lambda x: (x.get("presentation_date") or "", x.get("sort_order") or 0, x.get("title") or ""))

    if max_items:
        items = items[:max_items]
        print(f"Limited processing to first {max_items} item(s).")

    summit_index_entries = []

    for idx, item in enumerate(items, start=1):
        prog_id = item["id"]
        title = item.get("title") or f"Talk {idx}"
        speaker = item.get("speakers") or "Biohacker Center"
        premiered = item.get("presentation_date") or event_date
        embed_code = item.get("embed_code") or ""
        presentation_url = item.get("presentation_url") or ""
        description = item.get("description") or ""

        vimeo_url = extract_vimeo_url(embed_code, presentation_url)

        # Fetch AI data for this talk
        ai_rows = query_dicole_db("program_ai_data", columns="*", filters=[["eq", "program_id", prog_id]])
        ai_data = ai_rows[0] if ai_rows else {}

        clean_speaker = sanitize_filename(speaker)
        clean_title = sanitize_filename(title)
        base_filename = f"Biohacker Summit - S{season_year}E{idx:02d} - {clean_speaker} - {clean_title}"

        video_path = season_dir / f"{base_filename}.mp4"
        nfo_path = season_dir / f"{base_filename}.nfo"
        thumb_path = season_dir / f"{base_filename}-thumb.jpg"
        obsidian_note_path = obsidian_summit_dir / f"S{season_year}E{idx:02d} - {clean_speaker} - {clean_title}.md"

        print(f"\n[{idx}/{len(items)}] {speaker} - {title}")
        print(f"     Vimeo Stream: {vimeo_url or 'None'}")
        print(f"     AI Summary: {'Yes' if ai_data.get('ai_summary') else 'No'} | Transcript: {'Yes' if ai_data.get('ai_edited_transcript') else 'No'}")

        if dry_run:
            continue

        # 1. Fetch Episode Thumbnail
        if vimeo_url and not thumb_path.exists():
            fetch_vimeo_thumbnail(vimeo_url, thumb_path)

        # 2. Write Jellyfin Episode NFO
        episode_nfo_content = build_jellyfin_episode_nfo(
            title=title,
            show_title="Biohacker Summit",
            season_year=season_year,
            episode_num=idx,
            speaker=speaker,
            premiered=premiered,
            ai_data=ai_data,
            description=description
        )
        nfo_path.write_text(episode_nfo_content, encoding="utf-8")

        # 2. Write Obsidian Markdown Note
        rel_media_path = str(video_path.relative_to(media_dir.parent)) if media_dir.parent in video_path.parents else str(video_path)
        obsidian_note_content = build_obsidian_note(
            title=title,
            speaker=speaker,
            event_title=event_title,
            season_year=season_year,
            episode_num=idx,
            premiered=premiered,
            vimeo_url=vimeo_url,
            presentation_url=presentation_url,
            ai_data=ai_data,
            description=description,
            media_rel_path=rel_media_path
        )
        obsidian_note_path.write_text(obsidian_note_content, encoding="utf-8")

        summit_index_entries.append({
            "episode": idx,
            "speaker": speaker,
            "title": title,
            "note_rel": str(obsidian_note_path.relative_to(vault_dir / "Fitness and Sports")),
            "one_liner": ai_data.get("ai_one_liner") or "",
            "has_video": bool(vimeo_url)
        })

        # 3. Download Video via yt-dlp if requested
        if download_videos and vimeo_url:
            download_video_yt_dlp(vimeo_url, video_path, quality=quality)
        elif not vimeo_url:
            print(f"[INFO] No Vimeo video embed found for this item; metadata and notes preserved.")

    # Write Summit Master Index in Obsidian
    if not dry_run and summit_index_entries:
        index_file = vault_dir / "Fitness and Sports" / "Biohacker Summits" / f"{event_title}.md"
        index_content = f"""---
type: resource-index
category: health
event: "{event_title}"
year: {season_year}
city: "{city}"
total_talks: {len(summit_index_entries)}
date: {event_date.split('T')[0] if event_date else ''}
tags: [biohacking, summit, index, year/{season_year}]
---

# {event_title}

> [!info] Conference Overview
> **Year:** {season_year} | **Location:** {city}  
> **Total Sessions:** {len(summit_index_entries)}  
> **Jellyfin Path:** `tv/Biohacker Summit/{season_name}/`  

## Sessions & Keynotes

| Ep | Speaker | Talk Title | Synopsis | Note |
| :---: | :--- | :--- | :--- | :--- |
"""
        for e in summit_index_entries:
            note_link = f"[[Fitness and Sports/{e['note_rel']}|Notes]]"
            synopsis = e['one_liner'].replace("|", "-")
            index_content += f"| S{season_year}E{e['episode']:02d} | **{e['speaker']}** | {e['title']} | {synopsis} | {note_link} |\n"

        index_file.write_text(index_content, encoding="utf-8")
        print(f"\n[CREATED] Master Index Note: {index_file}")


def main():
    parser = argparse.ArgumentParser(description="Dicole.AI / Biohacker Summit Archiver & Jellyfin Integration Pipeline")
    default_vault = Path(os.environ.get("OBSIDIAN_VAULT_DIR", Path.home() / "vault_sync" / "ObsidianVault"))
    default_media = Path(os.environ.get("MEDIA_LIBRARY_DIR", "/mnt/media_library/media/tv"))
    parser.add_argument("--vault-dir", type=Path, default=default_vault,
                        help="Path to Obsidian vault directory.")
    parser.add_argument("--media-dir", type=Path, default=default_media,
                        help="Path to TV media directory (e.g. /mnt/media_library/media/tv).")
    parser.add_argument("--list-events", action="store_true", help="List all available summit events in Dicole.AI.")
    parser.add_argument("--event-id", type=str, help="Specific Dicole event ID to download.")
    parser.add_argument("--year", type=int, help="Target summit year (e.g. 2024).")
    parser.add_argument("--city", type=str, help="Target summit city filter (e.g. Helsinki).")
    parser.add_argument("--max-items", type=int, default=None, help="Maximum number of talks to download/process.")
    parser.add_argument("--skip-video", action="store_true", help="Generate NFO and Obsidian notes without downloading video files.")
    parser.add_argument("--quality", type=str, default=None, help="Custom yt-dlp format string.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect actions without writing files or downloading.")

    args = parser.parse_args()

    if args.list_events:
        summits = get_all_summits()
        print(f"\nAvailable Biohacker Summits ({len(summits)} found):")
        print(f"{'ID':<38} | {'Year':<6} | {'Title'}")
        print("-" * 80)
        for s in summits:
            title = s.get("title") or ""
            date = s.get("start_date") or s.get("created_at") or ""
            year = date[:4] if date else "----"
            print(f"{s['id']:<38} | {year:<6} | {title}")
        return

    # Find matching event
    target_event = None
    summits = get_all_summits()

    if args.event_id:
        for s in summits:
            if s["id"] == args.event_id:
                target_event = s
                break
        if not target_event:
            print(f"[ERROR] Event with ID '{args.event_id}' not found.", file=sys.stderr)
            sys.exit(1)
    elif args.year:
        candidates = []
        for s in summits:
            title = s.get("title", "")
            date = s.get("start_date") or s.get("created_at") or ""
            if str(args.year) in title or str(args.year) in date:
                if not args.city or args.city.lower() in title.lower():
                    candidates.append(s)
        if not candidates:
            print(f"[ERROR] No event matching year {args.year} (city filter: {args.city}) found.", file=sys.stderr)
            sys.exit(1)
        target_event = candidates[0]
    else:
        # Default to Biohacker Summit 2024 Helsinki
        for s in summits:
            if "2024" in s.get("title", "") and "helsinki" in s.get("title", "").lower():
                target_event = s
                break
        if not target_event and summits:
            target_event = summits[0]

    if not target_event:
        print("[ERROR] No target event found.", file=sys.stderr)
        sys.exit(1)

    process_summit(
        event=target_event,
        media_dir=args.media_dir,
        vault_dir=args.vault_dir,
        download_videos=not args.skip_video,
        max_items=args.max_items,
        quality=args.quality,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
