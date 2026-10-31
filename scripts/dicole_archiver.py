#!/usr/bin/env python3
"""
Dicole.AI / Biohacker Summit Archiver & Jellyfin Integration Pipeline
====================================================================
Extracts Biohacker & Hololife Summit recordings (2016–2025), pre-generated AI
summaries, key takeaways, habits, quotes, and transcripts from Dicole.AI,
organises them as a structured TV series for Jellyfin with rich Kodi/Jellyfin
XML (.nfo) metadata, bespoke summit artwork, and 1280x720 slide thumbnails, and
exports interlinked research notes into an Obsidian vault.

Author: Antigravity Autonomous Nightly Operator
License: MIT
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

API_DATABASE_URL = "https://dicole.ai/api/database"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FFMPEG_BIN = Path.home() / ".local" / "bin" / "ffmpeg"
YT_DLP_BIN = Path.home() / ".local" / "bin" / "yt-dlp"

# Primary summit per year receives <seasonnumber>YYYY</seasonnumber>;
# secondary summits in multi-summit years receive <seasonnumber>YYYYMM</seasonnumber>
# so Jellyfin Season.PresentationUniqueKey and Series.FilterEpisodesBySeason never collide.
PRIMARY_CITY_BY_YEAR = {
    2025: "Tallinn",
    2024: "Helsinki",
    2023: "Amsterdam",
    2022: "Amsterdam",
    2020: "Helsinki",
    2019: "Helsinki",
    2018: "Stockholm",
    2017: "Helsinki",
    2016: "Helsinki",
}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Sanitize string for filesystem paths across Linux/Windows/NFS."""
    if not name:
        return "Untitled"
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", str(name))
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip(" .-_")
    return clean or "Untitled"


def clean_xml_text(val: str) -> str:
    """Strip invalid XML 1.0 control characters."""
    if not val:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(val))


def query_dicole_db(
    table: str,
    columns: str = "*",
    filters: list = None,
    limit: int = None,
    public: bool = True,
) -> list:
    """Query Dicole.AI internal PostgREST database proxy."""
    payload = {
        "op": "from",
        "method": "select",
        "table": table,
        "columns": columns,
        "filters": filters or [],
        "public": public,
    }
    if limit is not None:
        payload["limit"] = limit

    req = urllib.request.Request(
        API_DATABASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
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


def get_all_summits(biohacker_only: bool = False) -> list:
    """Fetch all events matching 'summit' or 'biohack'."""
    events = query_dicole_db("events", columns="*")
    summits = []
    for ev in events:
        title = ev.get("title") or ""
        t_low = title.lower()
        if biohacker_only:
            if ("biohacker summit" in t_low or "hololife summit" in t_low) and "fintech" not in t_low:
                summits.append(ev)
        else:
            if "summit" in t_low or "biohack" in t_low:
                summits.append(ev)
    summits.sort(key=lambda x: x.get("start_date") or x.get("created_at") or "", reverse=True)
    return summits


def parse_summit_season_info(event: dict) -> dict:
    """Compute canonical year, month, city, season folder name, unique season number, and sort title."""
    event_title = event.get("title") or "Biohacker Summit"
    event_date = event.get("start_date") or event.get("created_at") or ""

    year_match = re.search(r"\b(20\d\d)\b", event_title)
    if year_match:
        season_year = int(year_match.group(1))
    elif event_date and len(event_date) >= 4 and event_date[:4].isdigit():
        season_year = int(event_date[:4])
    else:
        season_year = 2024

    month = 10
    if event_date and len(event_date) >= 7 and event_date[5:7].isdigit():
        month = int(event_date[5:7])

    city_match = re.search(
        r"\b(Helsinki|Amsterdam|London|Stockholm|Tallinn|Toronto|Tokyo)\b",
        event_title,
        re.IGNORECASE,
    )
    city = city_match.group(1).capitalize() if city_match else "International"
    season_name = f"Season {season_year} - {city}"
    sort_title = f"{season_year}-{month:02d} - {city}"

    primary_city = PRIMARY_CITY_BY_YEAR.get(season_year)
    if primary_city and primary_city.lower() == city.lower():
        season_num = season_year
    elif primary_city is None:
        season_num = season_year
    else:
        season_num = season_year * 100 + month

    return {
        "season_year": season_year,
        "month": month,
        "city": city,
        "season_name": season_name,
        "sort_title": sort_title,
        "season_num": season_num,
    }


def extract_video_url(embed_code: str, presentation_url: str) -> str:
    """Extract Vimeo (preserving ?h= privacy hash if present) or YouTube video stream URL."""
    for src in (embed_code or "", presentation_url or ""):
        if not src:
            continue
        m = re.search(r'src=["\'](https?://player\.vimeo\.com/video/(\d+)[^"\']*)["\']', src)
        if m:
            raw_url = m.group(1).replace("&amp;", "&")
            vid = m.group(2)
            mh = re.search(r"[?&]h=([a-fA-F0-9]+)", raw_url)
            if mh:
                return f"https://player.vimeo.com/video/{vid}?h={mh.group(1)}"
            return f"https://player.vimeo.com/video/{vid}"
        m = re.search(r"vimeo\.com/(?:video/)?(\d+)(?:/([a-fA-F0-9]+)|\?h=([a-fA-F0-9]+))?", src)
        if m:
            vid = m.group(1)
            h_val = m.group(2) or m.group(3)
            if h_val:
                return f"https://player.vimeo.com/video/{vid}?h={h_val}"
            return f"https://player.vimeo.com/video/{vid}"
        m = re.search(
            r"(?:youtube\.com/embed/|youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})",
            src,
        )
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    return ""


def build_jellyfin_tvshow_nfo() -> str:
    """Generate top-level tvshow.nfo for Jellyfin."""
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Biohacker Summit</title>
  <originaltitle>Biohacker Summit</originaltitle>
  <showtitle>Biohacker Summit</showtitle>
  <plot>Recordings, keynote lectures, and masterclasses from the international Biohacker and HOLOLIFE Summits (2016–2025) focusing on human performance optimization, preventative health, longevity, sleep architecture, and circadian biology.</plot>
  <genre>Health &amp; Longevity</genre>
  <genre>Biohacking</genre>
  <genre>Science &amp; Technology</genre>
  <studio>Biohacker Center</studio>
</tvshow>
"""


def build_jellyfin_season_nfo(
    season_num: int,
    season_title: str,
    sort_title: str,
    event_desc: str = "",
) -> str:
    """Generate season.nfo for Jellyfin with unique seasonnumber and chronological sorttitle."""
    desc = clean_xml_text(event_desc) or f"Recordings and keynote lectures from {season_title}."
    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<season>
  <title>{xml_escape(clean_xml_text(season_title))}</title>
  <sorttitle>{xml_escape(clean_xml_text(sort_title))}</sorttitle>
  <seasonnumber>{season_num}</seasonnumber>
  <plot>{xml_escape(desc)}</plot>
</season>
"""


def format_plot_text(ai_data: dict, description: str = "") -> str:
    """Format the Jellyfin episode plot with a concise, readable AI summary."""
    summary = clean_xml_text(ai_data.get("ai_summary") or "").strip()
    one_liner = clean_xml_text(ai_data.get("ai_one_liner") or "").strip()
    desc_clean = clean_xml_text(description or "").strip()

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
    elif desc_clean:
        lines.append(desc_clean)

    return "\n\n".join(lines).strip()


def build_jellyfin_episode_nfo(
    title: str,
    show_title: str,
    season_num: int,
    episode_num: int,
    speaker: str,
    premiered: str,
    ai_data: dict,
    description: str = "",
) -> str:
    """Generate episode .nfo for Jellyfin / Kodi."""
    plot = format_plot_text(ai_data, description)
    outline = clean_xml_text(ai_data.get("ai_one_liner") or (description or "")[:200])
    date_str = premiered.split("T")[0] if premiered else ""

    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>{xml_escape(clean_xml_text(title))}</title>
  <showtitle>{xml_escape(clean_xml_text(show_title))}</showtitle>
  <season>{season_num}</season>
  <episode>{episode_num}</episode>
  <outline>{xml_escape(outline)}</outline>
  <plot><![CDATA[{plot}]]></plot>
  <director>{xml_escape(clean_xml_text(speaker))}</director>
  <credits>{xml_escape(clean_xml_text(speaker))}</credits>
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
    video_url: str,
    presentation_url: str,
    ai_data: dict,
    description: str = "",
    media_rel_path: str = "",
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
        speaker_tag = re.sub(r"[^a-zA-Z0-9_]", "", speaker.lower().replace(" ", "_"))
        if speaker_tag:
            tags.append(f"speaker/{speaker_tag}")

    safe_event_title = str(event_title).replace('"', '\\"')
    safe_speaker = str(speaker).replace('"', '\\"')

    yaml_header = f"""---
type: resource
category: health
event: "{safe_event_title}"
season: {season_year}
episode: {episode_num}
speaker: "{safe_speaker}"
date: {date_str}
vimeo_url: "{video_url}"
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
    if video_url:
        links.append(f"- [Video Stream]({video_url})")
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


def fetch_video_thumbnail(
    video_url: str,
    output_path: Path,
    fallback_backdrop: Path = None,
) -> bool:
    """Fetch high-resolution slide thumbnail via Vimeo oEmbed or YouTube i.ytimg.com."""
    if output_path.exists() and output_path.stat().st_size > 5_000:
        return True

    # 1. Vimeo oEmbed
    m_vimeo = re.search(r"/video/(\d+)(?:\?h=([a-fA-F0-9]+))?", video_url or "")
    if m_vimeo:
        vid = m_vimeo.group(1)
        h_hash = m_vimeo.group(2)
        target_vimeo = f"https://vimeo.com/{vid}/{h_hash}" if h_hash else f"https://player.vimeo.com/video/{vid}"
        oembed_url = f"https://vimeo.com/api/oembed.json?url={target_vimeo}"
        raw_thumb = ""
        try:
            req = urllib.request.Request(oembed_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
                raw_thumb = data.get("thumbnail_url", "")
                if raw_thumb:
                    high_thumb = raw_thumb.split("_")[0] + "_1280.jpg"
                    req_img = urllib.request.Request(high_thumb, headers={"User-Agent": DEFAULT_USER_AGENT})
                    with urllib.request.urlopen(req_img, timeout=10) as ir, open(output_path, "wb") as f:
                        f.write(ir.read())
                    return True
        except Exception:
            if raw_thumb:
                try:
                    req_fb = urllib.request.Request(raw_thumb, headers={"User-Agent": DEFAULT_USER_AGENT})
                    with urllib.request.urlopen(req_fb, timeout=10) as ir, open(output_path, "wb") as f:
                        f.write(ir.read())
                    return True
                except Exception:
                    pass

    # 2. YouTube i.ytimg.com
    m_yt = re.search(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]{11})", video_url or "")
    if m_yt:
        yid = m_yt.group(1)
        for variant in ("maxresdefault.jpg", "hqdefault.jpg", "mqdefault.jpg"):
            yt_thumb_url = f"https://i.ytimg.com/vi/{yid}/{variant}"
            try:
                req_img = urllib.request.Request(yt_thumb_url, headers={"User-Agent": DEFAULT_USER_AGENT})
                with urllib.request.urlopen(req_img, timeout=10) as ir:
                    img_bytes = ir.read()
                    if len(img_bytes) > 2_000:
                        output_path.write_bytes(img_bytes)
                        return True
            except Exception:
                continue

    # 3. Fallback to season backdrop if available
    if fallback_backdrop and fallback_backdrop.exists() and not output_path.exists():
        shutil.copyfile(fallback_backdrop, output_path)
        return True

    return False


def setup_summit_artwork(event: dict, show_dir: Path, season_dir: Path):
    """Generate show & season-specific posters (1000x1500) and fanart backgrounds (1920x1080)."""
    logo_url = event.get("event_logo")
    if not logo_url:
        return

    ext = ".png" if logo_url.lower().endswith(".png") else ".jpg"
    cache_file = Path("/tmp") / f"event_logo_{event.get('id', 'default')}{ext}"
    if not cache_file.exists() or cache_file.stat().st_size < 1_000:
        try:
            req = urllib.request.Request(logo_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp, open(cache_file, "wb") as f:
                f.write(resp.read())
        except Exception as e:
            print(f"[WARN] Failed downloading event logo {logo_url}: {e}", file=sys.stderr)
            return

    if not cache_file.exists() or not FFMPEG_BIN.exists():
        return

    poster_vf = (
        "crop='min(iw,ih*2/3)':'min(ih,iw*3/2)':"
        "'(iw-min(iw,ih*2/3))/2':'(ih-min(ih,iw*3/2))/2',"
        "scale=1000:1500:flags=lanczos,format=yuvj420p"
    )
    fanart_vf = (
        "scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1920:1080,format=yuvj420p"
    )

    # 1. Season-specific Poster & Fanart (generated from THIS summit's event_logo)
    season_poster = season_dir / "poster.jpg"
    season_fanart = season_dir / "fanart.jpg"

    if not season_poster.exists():
        cmd = [
            str(FFMPEG_BIN), "-y", "-i", str(cache_file),
            "-frames:v", "1", "-q:v", "2", "-vf", poster_vf,
            str(season_poster),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if season_poster.exists():
            shutil.copyfile(season_poster, season_dir / "season-poster.jpg")
            shutil.copyfile(season_poster, season_dir / "folder.jpg")

    if not season_fanart.exists():
        cmd = [
            str(FFMPEG_BIN), "-y", "-i", str(cache_file),
            "-frames:v", "1", "-q:v", "2", "-vf", fanart_vf,
            str(season_fanart),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if season_fanart.exists():
            shutil.copyfile(season_fanart, season_dir / "backdrop.jpg")

    # 2. Top-level Show Poster & Fanart (if not already present)
    show_poster = show_dir / "poster.jpg"
    show_fanart = show_dir / "fanart.jpg"
    if not show_poster.exists() and season_poster.exists():
        shutil.copyfile(season_poster, show_poster)
        shutil.copyfile(season_poster, show_dir / "folder.jpg")
    if not show_fanart.exists() and season_fanart.exists():
        shutil.copyfile(season_fanart, show_fanart)
        shutil.copyfile(season_fanart, show_dir / "backdrop.jpg")


def mux_audio_presentation_to_mp4(
    audio_url: str,
    backdrop_path: Path,
    output_path: Path,
) -> bool:
    """Download audio-only presentation (.m4a/.mp3) and mux with summit backdrop into 1080p MP4."""
    if output_path.exists() and output_path.stat().st_size > 1_000_000:
        return True
    if not backdrop_path or not backdrop_path.exists() or not FFMPEG_BIN.exists():
        return False

    audio_tmp = output_path.with_suffix(".tmp_audio")
    temp_mp4 = output_path.with_suffix(".tmp.mp4")
    try:
        print(f"[AUDIO->MP4] Downloading audio asset {audio_url} -> {output_path.name}...")
        req = urllib.request.Request(audio_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(audio_tmp, "wb") as f:
            shutil.copyfileobj(resp, f)

        cmd = [
            str(FFMPEG_BIN), "-y",
            "-loop", "1", "-framerate", "1", "-i", str(backdrop_path),
            "-i", str(audio_tmp),
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(temp_mp4),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if temp_mp4.exists() and temp_mp4.stat().st_size > 500_000:
            temp_mp4.rename(output_path)
            print(f"[SUCCESS] Muxed audio session: {output_path.name} ({output_path.stat().st_size / (1024*1024):.1f} MB)")
            return True
    except Exception as e:
        print(f"[ERROR] Failed muxing audio presentation {audio_url}: {e}", file=sys.stderr)
    finally:
        if audio_tmp.exists():
            audio_tmp.unlink()
        if temp_mp4.exists():
            temp_mp4.unlink()
    return False


def download_video_yt_dlp(
    video_url: str,
    output_path: Path,
    quality: str = None,
    presentation_url: str = None,
    backdrop_path: Path = None,
) -> bool:
    """Download video using yt-dlp with Vimeo/YouTube support and +faststart MP4 muxing."""
    if output_path.exists() and output_path.stat().st_size > 5_000_000:
        print(f"[SKIP] Video already exists: {output_path.name} ({output_path.stat().st_size / (1024*1024):.1f} MB)")
        return True

    if not video_url:
        if presentation_url and re.search(r"\.(m4a|mp3|wav|aac)(\?|$)", presentation_url, re.IGNORECASE):
            return mux_audio_presentation_to_mp4(presentation_url, backdrop_path, output_path)
        return False

    temp_path = output_path.with_suffix(".tmp.mp4")
    for stale in output_path.parent.glob(f"{output_path.stem}.tmp*"):
        try:
            stale.unlink()
        except OSError:
            pass

    quality_fmt = quality or "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    yt_dlp_exe = str(YT_DLP_BIN) if YT_DLP_BIN.exists() else "yt-dlp"

    cmd = [
        yt_dlp_exe,
        "--ffmpeg-location", str(FFMPEG_BIN.parent),
        "-f", quality_fmt,
        "--merge-output-format", "mp4",
        "--ppa", "Merger+ffmpeg_o1:-movflags +faststart",
        "-N", "8",
        "--retries", "10",
        "--fragment-retries", "10",
        "--no-mtime",
        "--no-playlist",
        "-o", str(temp_path),
    ]

    if "vimeo.com" in video_url:
        cmd.extend(["--referer", "https://dicole.ai/"])
    elif "youtube.com" in video_url or "youtu.be" in video_url:
        if shutil.which("node"):
            cmd.extend(["--js-runtimes", "node"])

    cmd.append(video_url)

    print(f"[DOWNLOADING] {video_url} -> {output_path.name}...", flush=True)
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        if temp_path.exists() and temp_path.stat().st_size > 1_000_000:
            temp_path.rename(output_path)
            print(
                f"[SUCCESS] Downloaded: {output_path.name} ({output_path.stat().st_size / (1024*1024):.1f} MB)",
                flush=True,
            )
            return True
        else:
            print(f"[ERROR] Output file too small or missing after download: {temp_path}", file=sys.stderr)
            return False
    except subprocess.CalledProcessError as e:
        out_tail = (e.stdout or "")[-500:]
        print(f"[ERROR] yt-dlp failed for {video_url}:\n{out_tail}", file=sys.stderr)
        for stale in output_path.parent.glob(f"{output_path.stem}.tmp*"):
            try:
                stale.unlink()
            except OSError:
                pass
        return False


def write_master_summits_overview(vault_dir: Path, summaries: list):
    """Write top-level Obsidian index linking all archived Biohacker & Hololife Summits."""
    base_dir = vault_dir / "Fitness and Sports" / "Biohacker Summits"
    base_dir.mkdir(parents=True, exist_ok=True)
    overview_file = base_dir / "00_Biohacker_Summits_Index.md"

    total_talks = sum(s["talks"] for s in summaries)
    lines = [
        "---",
        "type: resource-hub",
        "category: health",
        f"total_summits: {len(summaries)}",
        f"total_talks: {total_talks}",
        "tags: [biohacking, longevity, summit, moc]",
        "---",
        "",
        "# Biohacker & HOLOLIFE Summits Archive (2016–2025)",
        "",
        "> [!info] Complete Multi-Year Conference Library",
        f"> **Archived Summits:** {len(summaries)} (2016–2025)  ",
        f"> **Total Keynote Lectures & Sessions:** {total_talks}  ",
        "> **Jellyfin Series Path:** `/media/tv/Biohacker Summit/`  ",
        "",
        "## Conference Editions",
        "",
        "| Year | Date | Summit | Location | Jellyfin Season | Sessions | Index Note |",
        "| :---: | :---: | :--- | :--- | :--- | :---: | :--- |",
    ]
    for s in summaries:
        link = f"[[Fitness and Sports/Biohacker Summits/{s['event_title']}|{s['event_title']}]]"
        lines.append(
            f"| **{s['year']}** | {s['date']} | {s['event_title']} | {s['city']} | `{s['season_name']}` (`#{s['season_num']}`) | {s['talks']} | {link} |"
        )

    overview_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[CREATED] Top-Level Summits Overview: {overview_file}")


def process_summit(
    event: dict,
    media_dir: Path,
    vault_dir: Path,
    download_videos: bool = True,
    max_items: int = None,
    quality: str = None,
    dry_run: bool = False,
) -> dict:
    """Process all talks for an individual Biohacker/Hololife Summit."""
    event_id = event["id"]
    event_title = (event.get("title") or "Biohacker Summit").strip()
    event_date = event.get("start_date") or event.get("created_at") or ""

    info = parse_summit_season_info(event)
    season_year = info["season_year"]
    city = info["city"]
    season_name = info["season_name"]
    sort_title = info["sort_title"]
    season_num = info["season_num"]

    print(f"\n========================================================", flush=True)
    print(f"Processing Event: {event_title}", flush=True)
    print(f"Folder: {season_name} | Season #: {season_num} | Sort: {sort_title}", flush=True)
    print(f"========================================================", flush=True)

    show_dir = media_dir / "Biohacker Summit"
    season_dir = show_dir / season_name
    obsidian_summit_dir = vault_dir / "Fitness and Sports" / "Biohacker Summits" / f"{season_year} {city}"

    if not dry_run:
        season_dir.mkdir(parents=True, exist_ok=True)
        obsidian_summit_dir.mkdir(parents=True, exist_ok=True)

        tvshow_nfo_path = show_dir / "tvshow.nfo"
        tvshow_nfo_path.write_text(build_jellyfin_tvshow_nfo(), encoding="utf-8")

        season_nfo_path = season_dir / "season.nfo"
        season_nfo_path.write_text(
            build_jellyfin_season_nfo(season_num, season_name, sort_title, event.get("description", "")),
            encoding="utf-8",
        )

        setup_summit_artwork(event, show_dir, season_dir)

    items = query_dicole_db("program_items", columns="*", filters=[["eq", "event_id", event_id]])
    items.sort(key=lambda x: (x.get("presentation_date") or "", x.get("sort_order") or 0, x.get("title") or ""))

    if max_items:
        items = items[:max_items]

    print(f"Discovered {len(items)} talks in '{event_title}'.", flush=True)

    # Batch-fetch all AI data for this summit in a single PostgREST request
    ai_by_prog_id = {}
    if items:
        prog_ids = [it["id"] for it in items]
        ai_rows = query_dicole_db("program_ai_data", columns="*", filters=[["in", "program_id", prog_ids]])
        for r in ai_rows:
            pid = r.get("program_id")
            if pid and pid not in ai_by_prog_id:
                ai_by_prog_id[pid] = r

    summit_index_entries = []
    thumb_tasks = []
    video_tasks = []
    backdrop_path = season_dir / "backdrop.jpg"

    for idx, item in enumerate(items, start=1):
        prog_id = item["id"]
        title = (item.get("title") or f"Talk {idx}").strip()
        speaker = (item.get("speakers") or "Biohacker Center").strip()
        premiered = item.get("presentation_date") or event_date
        embed_code = item.get("embed_code") or ""
        presentation_url = item.get("presentation_url") or ""
        description = item.get("description") or ""

        video_url = extract_video_url(embed_code, presentation_url)
        ai_data = ai_by_prog_id.get(prog_id, {})

        clean_speaker = sanitize_filename(speaker)
        clean_title = sanitize_filename(title)
        base_filename = f"Biohacker Summit - S{season_year}E{idx:02d} - {clean_speaker} - {clean_title}"

        video_path = season_dir / f"{base_filename}.mp4"
        nfo_path = season_dir / f"{base_filename}.nfo"
        thumb_path = season_dir / f"{base_filename}-thumb.jpg"
        obsidian_note_path = obsidian_summit_dir / f"S{season_year}E{idx:02d} - {clean_speaker} - {clean_title}.md"

        if dry_run:
            print(f"  [DRY-RUN] [{idx:02d}/{len(items):02d}] {speaker} - {title} ({video_url or 'audio'})")
            continue

        # Write Jellyfin Episode NFO
        episode_nfo_content = build_jellyfin_episode_nfo(
            title=title,
            show_title="Biohacker Summit",
            season_num=season_num,
            episode_num=idx,
            speaker=speaker,
            premiered=premiered,
            ai_data=ai_data,
            description=description,
        )
        nfo_path.write_text(episode_nfo_content, encoding="utf-8")

        # Write Obsidian Markdown Note
        rel_media_path = (
            str(video_path.relative_to(media_dir.parent))
            if media_dir.parent in video_path.parents
            else str(video_path)
        )
        obsidian_note_content = build_obsidian_note(
            title=title,
            speaker=speaker,
            event_title=event_title,
            season_year=season_year,
            episode_num=idx,
            premiered=premiered,
            video_url=video_url,
            presentation_url=presentation_url,
            ai_data=ai_data,
            description=description,
            media_rel_path=rel_media_path,
        )
        obsidian_note_path.write_text(obsidian_note_content, encoding="utf-8")

        if not thumb_path.exists():
            thumb_tasks.append((video_url, thumb_path, backdrop_path))

        if download_videos:
            video_tasks.append((idx, len(items), speaker, title, video_url, video_path, presentation_url))

        summit_index_entries.append({
            "episode": idx,
            "speaker": speaker,
            "title": title,
            "note_rel": str(obsidian_note_path.relative_to(vault_dir / "Fitness and Sports")),
            "one_liner": ai_data.get("ai_one_liner") or "",
            "has_video": bool(video_url or presentation_url),
        })

    # Fetch missing episode slide thumbnails in parallel
    if not dry_run and thumb_tasks:
        print(f"Fetching {len(thumb_tasks)} episode slide thumbnails in parallel...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch_video_thumbnail, u, p, b) for u, p, b in thumb_tasks]
            concurrent.futures.wait(futures)

    # Write Summit Master Index in Obsidian
    if not dry_run and summit_index_entries:
        index_file = vault_dir / "Fitness and Sports" / "Biohacker Summits" / f"{event_title}.md"
        safe_event_title = event_title.replace('"', '\\"')
        index_content = f"""---
type: resource-index
category: health
event: "{safe_event_title}"
year: {season_year}
city: "{city}"
season_number: {season_num}
total_talks: {len(summit_index_entries)}
date: {event_date.split('T')[0] if event_date else ''}
tags: [biohacking, summit, index, year/{season_year}]
---

# {event_title}

> [!info] Conference Overview
> **Year:** {season_year} | **Location:** {city}  
> **Total Sessions:** {len(summit_index_entries)}  
> **Jellyfin Path:** `tv/Biohacker Summit/{season_name}/` (Season `{season_num}`)  
> **All Summits:** [[Fitness and Sports/Biohacker Summits/00_Biohacker_Summits_Index|Biohacker & HOLOLIFE Summits Archive]]

## Sessions & Keynotes

| Ep | Speaker | Talk Title | Synopsis | Note |
| :---: | :--- | :--- | :--- | :--- |
"""
        for e in summit_index_entries:
            note_link = f"[[Fitness and Sports/{e['note_rel']}|Notes]]"
            synopsis = (e["one_liner"] or "").replace("|", "-").replace("\n", " ").strip()
            safe_title = e["title"].replace("|", "-")
            safe_spk = e["speaker"].replace("|", "-")
            index_content += f"| S{season_year}E{e['episode']:02d} | **{safe_spk}** | {safe_title} | {synopsis} | {note_link} |\n"

        index_file.write_text(index_content, encoding="utf-8")
        print(f"[CREATED] Master Index Note: {index_file}", flush=True)

    # Download videos if requested
    if not dry_run and download_videos and video_tasks:
        for idx, total, speaker, title, video_url, video_path, presentation_url in video_tasks:
            print(f"[{idx:02d}/{total:02d}] {speaker} - {title}", flush=True)
            download_video_yt_dlp(
                video_url=video_url,
                output_path=video_path,
                quality=quality,
                presentation_url=presentation_url,
                backdrop_path=backdrop_path,
            )

    return {
        "event_id": event_id,
        "event_title": event_title,
        "year": season_year,
        "date": event_date.split("T")[0] if event_date else f"{season_year}",
        "city": city,
        "season_name": season_name,
        "season_num": season_num,
        "talks": len(items),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Dicole.AI / Biohacker Summit Archiver & Jellyfin Integration Pipeline"
    )
    default_vault = Path(os.environ.get("OBSIDIAN_VAULT_DIR", Path.home() / "vault_sync" / "ObsidianVault"))
    default_media = Path(os.environ.get("MEDIA_LIBRARY_DIR", "/mnt/media_library/media/tv"))
    parser.add_argument("--vault-dir", type=Path, default=default_vault, help="Path to Obsidian vault directory.")
    parser.add_argument("--media-dir", type=Path, default=default_media, help="Path to TV media directory.")
    parser.add_argument("--list-events", action="store_true", help="List all available summit events in Dicole.AI.")
    parser.add_argument("--all-summits", action="store_true", help="Archive all 16 Biohacker & HOLOLIFE Summits (2016–2025).")
    parser.add_argument("--event-id", type=str, help="Specific Dicole event ID to download.")
    parser.add_argument("--year", type=int, help="Target summit year (e.g. 2024).")
    parser.add_argument("--city", type=str, help="Target summit city filter (e.g. Helsinki).")
    parser.add_argument("--max-items", type=int, default=None, help="Maximum number of talks to process per summit.")
    parser.add_argument("--skip-video", action="store_true", help="Generate artwork, thumbnails, NFOs, and Obsidian notes without video downloads.")
    parser.add_argument("--quality", type=str, default=None, help="Custom yt-dlp format string.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect actions without writing files or downloading.")

    args = parser.parse_args()

    if args.list_events:
        summits = get_all_summits(biohacker_only=False)
        print(f"\nAvailable Dicole Summits & Courses ({len(summits)} found):")
        print(f"{'ID':<38} | {'Year':<6} | {'Season #':<8} | {'Title'}")
        print("-" * 92)
        for s in summits:
            info = parse_summit_season_info(s)
            print(f"{s['id']:<38} | {info['season_year']:<6} | {info['season_num']:<8} | {s.get('title') or ''}")
        return

    if args.all_summits:
        summits = get_all_summits(biohacker_only=True)
        print(f"=== Pass 1: Generating Artwork, Thumbnails, Jellyfin NFOs & Obsidian Notes for {len(summits)} Summits ===", flush=True)
        summaries = []
        for s in summits:
            summary = process_summit(
                event=s,
                media_dir=args.media_dir,
                vault_dir=args.vault_dir,
                download_videos=False,
                max_items=args.max_items,
                quality=args.quality,
                dry_run=args.dry_run,
            )
            summaries.append(summary)

        if not args.dry_run and summaries:
            write_master_summits_overview(args.vault_dir, summaries)

        if not args.skip_video and not args.dry_run:
            print(f"\n=== Pass 2: Downloading 1080p Video Streams Across All {len(summits)} Summits ===", flush=True)
            for s in summits:
                process_summit(
                    event=s,
                    media_dir=args.media_dir,
                    vault_dir=args.vault_dir,
                    download_videos=True,
                    max_items=args.max_items,
                    quality=args.quality,
                    dry_run=False,
                )
        return

    # Single-summit mode
    target_event = None
    summits = get_all_summits(biohacker_only=False)

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
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
