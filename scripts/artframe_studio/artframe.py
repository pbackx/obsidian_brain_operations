"""ArtFrame Studio - a self-hosted replacement for the SwitchBot AI Studio subscription.

Core library (no Flask imports here):

* ``SwitchBotClient``  - SwitchBot v1.1 OpenAPI client (HMAC-SHA256 signed) that can push
                         an image to a first-generation AI Art Frame via ``uploadImage``.
* ``EinkPipeline``     - ffmpeg-only image pipeline: normalise a photo, remix it with an
                         image-edit model, then crop/dither it to the frame's native
                         E Ink Spectra 6 six-colour palette.
* ``Remixer``          - thin OpenRouter client for image-to-image ("Nano Banana" style)
                         style transfer.
* ``Store``            - tiny SQLite gallery of every generation.

Only stdlib + ``requests`` + the static ``ffmpeg`` binary are required (no Pillow, no numpy),
so the whole service idles at a few MB RSS inside an LXC container.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import random
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ARTFRAME_DATA_DIR", APP_DIR / "data"))
DB_PATH = Path(os.environ.get("ARTFRAME_DB", DATA_DIR / "artframe.sqlite3"))
FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SWITCHBOT_BASE = os.environ.get("SWITCHBOT_API_BASE", "https://api.switch-bot.com/v1.1")

# Default frame geometry: first-generation 13.3" AI Art Frame is a 30 x 40 cm (3:4) portrait
# panel. Override with FRAME_WIDTH / FRAME_HEIGHT for the 7.3" or 31.5" models.
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "1200"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "1600"))

# Approximate E Ink Spectra 6 primaries (black, white, red, yellow, green, blue).
# The panel's real primaries are pigment based; these are deliberately saturated so that
# Floyd-Steinberg dithering lands on the intended ink after the panel renders it.
SPECTRA6 = [
    (0, 0, 0),
    (255, 255, 255),
    (214, 30, 30),
    (255, 206, 0),
    (0, 122, 62),
    (24, 62, 168),
]

DITHERS = ("floyd_steinberg", "bayer", "none")


def load_env_file(path: Path | str = APP_DIR / "artframe.env") -> dict[str, str]:
    """Read simple KEY="value" lines; never overrides variables already in the environment."""
    values: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------------------------------------------
# Style presets
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Style:
    key: str
    label: str
    emoji: str
    prompt: str


STYLES: dict[str, Style] = {
    s.key: s
    for s in [
        Style("comic", "Comic Book", "💥",
              "bold comic-book illustration with thick black ink outlines, flat cel shading, "
              "halftone dot texture and dramatic action-panel lighting"),
        Style("pointillism", "Pointillism", "🔵",
              "neo-impressionist pointillist painting made of clearly visible dots of pure pigment, "
              "Seurat style, luminous colour mixing, soft edges"),
        Style("manga", "Manga", "🈶",
              "black-and-white Japanese manga panel, clean confident line art, screentone shading, "
              "expressive inking, high contrast, no speech bubbles"),
        Style("cyberpunk", "Cyberpunk", "🌃",
              "gritty cyberpunk graphic-novel art, neon rim light, synthwave magenta and cyan accents, "
              "heavy black shadows, futuristic haze"),
        Style("watercolour", "Watercolour", "🎨",
              "loose expressive watercolour painting, wet-on-wet bleeding, visible paper grain, "
              "soft transparent washes, delicate ink accents"),
        Style("oilpainting", "Oil Painting", "🖌️",
              "classical oil painting with visible impasto brush strokes, rich chiaroscuro, "
              "museum-quality lighting, thick varnish glow"),
        Style("popart", "Pop Art", "🟡",
              "1960s pop-art screenprint, flat bold blocks of primary colour, Ben-Day dots, "
              "Warhol/Lichtenstein energy, graphic black keyline"),
        Style("ukiyoe", "Ukiyo-e", "🌊",
              "traditional Japanese ukiyo-e woodblock print, flat colour planes, "
              "crisp keyblock outlines, Hokusai wave motifs, washi paper texture"),
        Style("pencil", "Pencil Sketch", "✏️",
              "detailed graphite pencil sketch on textured paper, cross-hatching, "
              "soft smudged shading, monochrome"),
        Style("stainedglass", "Stained Glass", "🪟",
              "leaded stained-glass window, bold black came lines, jewel-toned translucent panes, "
              "glowing backlight"),
        Style("lowpoly", "Low Poly", "🔺",
              "flat low-poly vector illustration, faceted geometric planes of solid colour, "
              "minimal gradients, crisp silhouettes"),
        Style("artdeco", "Art Deco", "🗿",
              "Art Deco poster illustration, geometric gold and emerald shapes, "
              "symmetrical ornamental framing, 1920s travel-poster elegance"),
    ]
}

# Every remix prompt ends with these constraints: keep the photo recognisable, and keep the
# result printable on a six-ink e-paper panel.
PROMPT_SUFFIX = (
    " Keep the exact composition, framing, perspective and recognisable subjects of the source "
    "photo. Redraw it in the requested style. Use a limited palette that reads well on a "
    "six-colour E Ink Spectra 6 panel (black, white, red, yellow, green, blue): favour large "
    "flat areas of colour, high contrast and strong silhouettes, avoid tiny detail, soft "
    "gradients, bokeh and photographic noise. Do not add any text, captions, watermarks or "
    "borders."
)


def build_prompt(style_key: str, extra: str = "") -> str:
    style = STYLES.get(style_key) or STYLES["comic"]
    extra = (extra or "").strip()
    prompt = f"Restyle this photo as a {style.prompt}.{PROMPT_SUFFIX}"
    if extra:
        prompt += f" Additional art direction from the user: {extra}"
    return prompt


# --------------------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------------------

class ImageError(RuntimeError):
    pass


def _run_ffmpeg(args: list[str], timeout: int = 120) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise ImageError(proc.stderr.decode("utf-8", "replace").strip()[-800:] or "ffmpeg failed")


def palette_png(path: Path, colours: list[tuple[int, int, int]] = SPECTRA6) -> Path:
    """Materialise a 16x16 (256-entry) ffmpeg palette PNG from the given ink colours."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = path.with_suffix(".rgb")
    buf = bytearray()
    for i in range(256):
        r, g, b = colours[i % len(colours)]
        buf.extend((r, g, b))
    raw_path.write_bytes(bytes(buf))
    _run_ffmpeg(["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "16x16", "-i", str(raw_path),
                 "-frames:v", "1", str(path)])
    raw_path.unlink(missing_ok=True)
    return path


class EinkPipeline:
    """All image manipulation, delegated to the static ffmpeg binary."""

    def __init__(self, width: int = FRAME_WIDTH, height: int = FRAME_HEIGHT,
                 palette_dir: Path | None = None) -> None:
        self.width = width
        self.height = height
        self.palette_dir = Path(palette_dir or DATA_DIR)
        self._palette: Path | None = None
        self._lock = threading.Lock()

    @property
    def palette(self) -> Path:
        with self._lock:
            if self._palette is None or not self._palette.is_file():
                self._palette = palette_png(self.palette_dir / "spectra6_palette.png")
            return self._palette

    def normalise(self, src: Path, dest: Path, max_edge: int = 1600) -> Path:
        """Downscale + strip metadata so the photo upload stays small and private."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run_ffmpeg([
            "-i", str(src),
            "-vf", f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease",
            "-frames:v", "1", "-q:v", "3", str(dest),
        ])
        return dest

    def to_frame(self, src: Path, dest_jpeg: Path, eink_preview: Path | None = None,
                 dither: str = "floyd_steinberg", quantize_upload: bool = False,
                 quality: int = 5) -> Path:
        """Cover-crop to the panel geometry and write the JPEG that gets uploaded.

        The panel driver performs its own six-ink quantisation, so by default the uploaded
        file is the *full-colour* crop (avoiding double dithering). ``quantize_upload=True``
        ships the pre-dithered variant instead, for A/B tests on the real panel.
        ``eink_preview`` always receives a dithered PNG so the UI can show the expected inks.
        """
        if dither not in DITHERS:
            raise ValueError(f"unknown dither: {dither}")
        dest_jpeg.parent.mkdir(parents=True, exist_ok=True)
        crop = (f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{self.height}")
        effective = "floyd_steinberg" if dither == "none" else dither
        dithered = (f"{crop},format=rgb24[bg];[bg][1:v]paletteuse=dither={effective}")

        if quantize_upload:
            _run_ffmpeg(["-i", str(src), "-i", str(self.palette), "-filter_complex",
                         dithered, "-frames:v", "1", "-q:v", str(quality), str(dest_jpeg)])
        else:
            _run_ffmpeg(["-i", str(src), "-vf", crop, "-frames:v", "1",
                         "-q:v", str(quality), str(dest_jpeg)])

        if eink_preview is not None:
            eink_preview.parent.mkdir(parents=True, exist_ok=True)
            _run_ffmpeg(["-i", str(src), "-i", str(self.palette), "-filter_complex",
                         dithered, "-frames:v", "1", str(eink_preview)])
        return dest_jpeg

    @staticmethod
    def probe(path: Path) -> dict[str, Any]:
        out = subprocess.run(
            [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            raise ImageError(out.stderr.strip()[-400:])
        streams = json.loads(out.stdout or "{}").get("streams") or [{}]
        return {"width": streams[0].get("width"), "height": streams[0].get("height")}


def data_uri(path: Path, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


# --------------------------------------------------------------------------------------
# SwitchBot v1.1 client
# --------------------------------------------------------------------------------------

class SwitchBotError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class SwitchBotClient:
    """Minimal SwitchBot OpenAPI v1.1 client (AI Art Frame subset + device listing)."""

    def __init__(self, token: str, secret: str, device_id: str = "",
                 base_url: str = SWITCHBOT_BASE, timeout: int = 60,
                 uppercase_sign: bool = False) -> None:
        self.token = token
        self.secret = secret
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.uppercase_sign = uppercase_sign

    @classmethod
    def from_env(cls) -> "SwitchBotClient | None":
        token = _env("SWITCHBOT_TOKEN", "SWITCHBOT_OPEN_TOKEN")
        secret = _env("SWITCHBOT_SECRET", "SWITCHBOT_SECRET_KEY")
        if not token or not secret:
            return None
        return cls(token, secret, _env("SWITCHBOT_DEVICE_ID"),
                   uppercase_sign=_env("SWITCHBOT_UPPER_SIGN").lower() in ("1", "true", "yes"))

    # -- auth ---------------------------------------------------------------------------
    def headers(self, timestamp_ms: int | None = None, nonce: str | None = None) -> dict[str, str]:
        t = str(timestamp_ms if timestamp_ms is not None else int(round(time.time() * 1000)))
        n = nonce or str(uuid.uuid4())
        string_to_sign = f"{self.token}{t}{n}".encode()
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), string_to_sign, hashlib.sha256).digest()
        ).decode()
        if self.uppercase_sign:
            sign = sign.upper()
        return {
            "Authorization": self.token,
            "sign": sign,
            "nonce": n,
            "t": t,
            "Content-Type": "application/json; charset=utf8",
        }

    # -- transport ----------------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        import requests  # imported lazily so unit tests stay dependency-free

        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self.headers(),
                                json=payload, timeout=self.timeout)
        try:
            body = resp.json()
        except ValueError:
            raise SwitchBotError(f"non-JSON response (HTTP {resp.status_code})",
                                 resp.status_code, resp.text[:400])
        if resp.status_code >= 400:
            raise SwitchBotError(body.get("message") or f"HTTP {resp.status_code}",
                                 resp.status_code, body)
        if body.get("statusCode") not in (100, 0, None):
            raise SwitchBotError(body.get("message") or "SwitchBot error",
                                 body.get("statusCode"), body)
        return body.get("body") or {}

    # -- API ----------------------------------------------------------------------------
    def list_devices(self) -> dict[str, Any]:
        return self._request("GET", "/devices")

    def art_frames(self) -> list[dict[str, Any]]:
        body = self.list_devices()
        devices = (body.get("deviceList") or []) + (body.get("infraredRemoteList") or [])
        return [d for d in devices if "art frame" in str(d.get("deviceType", "")).lower()]

    def status(self, device_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/devices/{device_id or self.device_id}/status")

    def push_image(self, image: Path | str, device_id: str | None = None,
                   as_url: bool = False) -> dict[str, Any]:
        """Upload an image to the frame (newest image becomes the displayed artwork)."""
        image = Path(image)
        parameter: dict[str, str]
        if as_url:
            parameter = {"imageUrl": str(image)}
        else:
            parameter = {"imageBase64": data_uri(image, "image/jpeg")}
        return self._request("POST", f"/devices/{device_id or self.device_id}/commands",
                             {"command": "uploadImage", "parameter": parameter,
                              "commandType": "command"})

    def command(self, command: str, device_id: str | None = None,
                parameter: Any = "default") -> dict[str, Any]:
        return self._request("POST", f"/devices/{device_id or self.device_id}/commands",
                             {"command": command, "parameter": parameter,
                              "commandType": "command"})


# --------------------------------------------------------------------------------------
# OpenRouter image remixer
# --------------------------------------------------------------------------------------

class RemixError(RuntimeError):
    pass


@dataclass
class RemixResult:
    image_bytes: bytes
    mime: str
    model: str
    cost_usd: float
    seconds: float
    text: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)


class Remixer:
    DEFAULT_MODEL = os.environ.get("ARTFRAME_MODEL", "google/gemini-2.5-flash-image")

    def __init__(self, api_key: str | None = None, base_url: str = OPENROUTER_URL,
                 timeout: int = 180, referer: str = "http://localhost:8095") -> None:
        self.api_key = api_key or _env("OPENROUTER_API_KEY")
        self.base_url = base_url
        self.timeout = timeout
        self.referer = referer

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def remix(self, image: Path, style_key: str, extra: str = "",
              model: str | None = None, seed: int | None = None) -> RemixResult:
        if not self.configured:
            raise RemixError("OPENROUTER_API_KEY is not configured")
        import requests

        image = Path(image)
        mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        payload: dict[str, Any] = {
            "model": model or self.DEFAULT_MODEL,
            "modalities": ["image", "text"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(style_key, extra)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,"
                                          f"{base64.b64encode(image.read_bytes()).decode()}"}},
                ],
            }],
        }
        if seed is not None:
            payload["seed"] = seed

        started = time.time()
        resp = requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": self.referer,
                     "X-Title": "ArtFrame Studio"},
            json=payload, timeout=self.timeout,
        )
        elapsed = time.time() - started
        try:
            data = resp.json()
        except ValueError:
            raise RemixError(f"OpenRouter returned non-JSON (HTTP {resp.status_code})")
        if resp.status_code != 200:
            err = data.get("error") if isinstance(data, dict) else None
            raise RemixError((err or {}).get("message") or f"OpenRouter HTTP {resp.status_code}")

        message = (data.get("choices") or [{}])[0].get("message") or {}
        images = message.get("images") or []
        if not images:
            raise RemixError("model returned no image (try another style or model)")
        url = (images[0].get("image_url") or {}).get("url") or images[0].get("url") or ""
        if "," not in url:
            raise RemixError("model returned an unsupported image payload")
        header, b64 = url.split(",", 1)
        try:
            blob = base64.b64decode(b64)
        except (binascii.Error, ValueError) as exc:
            raise RemixError(f"could not decode returned image: {exc}") from exc
        mime_out = "image/png" if "png" in header else "image/jpeg"
        usage = data.get("usage") or {}
        return RemixResult(blob, mime_out, payload["model"],
                           float(usage.get("cost") or 0.0), elapsed,
                           text=(message.get("content") or "")[:500], raw_usage=usage)


# --------------------------------------------------------------------------------------
# Gallery store
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id          TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    style       TEXT NOT NULL,
    model       TEXT NOT NULL,
    extra       TEXT DEFAULT '',
    source_path TEXT,
    result_path TEXT,
    frame_path  TEXT,
    eink_path   TEXT,
    width       INTEGER,
    height      INTEGER,
    cost_usd    REAL DEFAULT 0,
    quantized   INTEGER DEFAULT 0,
    seconds     REAL DEFAULT 0,
    pushed_at   REAL,
    push_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_generations_created ON generations(created DESC);
"""


class Store:
    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def add(self, **fields: Any) -> str:
        fields.setdefault("id", uuid.uuid4().hex[:12])
        fields.setdefault("created", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as conn:
            conn.execute(f"INSERT INTO generations ({cols}) VALUES ({marks})",
                         tuple(fields.values()))
        return str(fields["id"])

    def get(self, gen_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 24) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM generations ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_pushed(self, gen_id: str, result: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE generations SET pushed_at=?, push_result=? WHERE id=?",
                         (time.time(), json.dumps(result)[:2000], gen_id))

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost, "
                "COALESCE(SUM(pushed_at IS NOT NULL),0) AS pushed FROM generations"
            ).fetchone()
        return dict(row) if row else {"n": 0, "cost": 0.0, "pushed": 0}


def random_style() -> str:
    return random.choice(sorted(STYLES))
