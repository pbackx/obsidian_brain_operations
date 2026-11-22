"""ArtFrame Studio - Flask front-end (Flask Vibe: server-rendered page + vanilla ES modules).

Run:  python3 app.py            (listens on 0.0.0.0:8095 by default)
Env:  ARTFRAME_PORT, ARTFRAME_HOST, ARTFRAME_DATA_DIR, OPENROUTER_API_KEY,
      SWITCHBOT_TOKEN, SWITCHBOT_SECRET, SWITCHBOT_DEVICE_ID
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

import artframe
from artframe import (DATA_DIR, EinkPipeline, Remixer, RemixError, Store, STYLES,
                      SwitchBotClient, SwitchBotError, ImageError, build_prompt,
                      load_env_file, random_style)

load_env_file()

PORT = int(os.environ.get("ARTFRAME_PORT", "8095"))
HOST = os.environ.get("ARTFRAME_HOST", "0.0.0.0")

MEDIA_DIR = DATA_DIR / "media"
UPLOAD_DIR = MEDIA_DIR / "source"
RESULT_DIR = MEDIA_DIR / "result"
FRAME_DIR = MEDIA_DIR / "frame"
EINK_DIR = MEDIA_DIR / "eink"
for _d in (UPLOAD_DIR, RESULT_DIR, FRAME_DIR, EINK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

store = Store()
pipeline = EinkPipeline()
remixer = Remixer()


def switchbot() -> SwitchBotClient | None:
    return SwitchBotClient.from_env()


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "created": row["created"],
        "style": row["style"],
        "style_label": STYLES.get(row["style"], STYLES["comic"]).label,
        "model": row["model"],
        "cost_usd": round(row.get("cost_usd") or 0.0, 4),
        "seconds": round(row.get("seconds") or 0.0, 1),
        "pushed_at": row.get("pushed_at"),
        "source_url": f"/media/source/{Path(row['source_path']).name}" if row.get("source_path") else None,
        "result_url": f"/media/result/{Path(row['result_path']).name}" if row.get("result_path") else None,
        "frame_url": f"/media/frame/{Path(row['frame_path']).name}" if row.get("frame_path") else None,
        "eink_url": f"/media/eink/{Path(row['eink_path']).name}" if row.get("eink_path") else None,
    }


@app.get("/")
def index():
    return render_template("index.html",
                           styles=[{"key": s.key, "label": s.label, "emoji": s.emoji}
                                   for s in STYLES.values()],
                           frame_width=pipeline.width, frame_height=pipeline.height,
                           frame_rotate=pipeline.rotate,
                           default_model=remixer.DEFAULT_MODEL,
                           remix_ready=remixer.configured,
                           switchbot_ready=switchbot() is not None)


@app.get("/api/health")
def health():
    client = switchbot()
    info = {"ok": True, "remix_ready": remixer.configured, "switchbot_ready": client is not None,
            "frame": {"width": pipeline.width, "height": pipeline.height,
                      "rotate": pipeline.rotate},
            "styles": len(STYLES), "stats": store.stats()}
    if client is not None:
        try:
            info["frames"] = client.art_frames()
        except SwitchBotError as exc:
            info["switchbot_error"] = str(exc)
    return jsonify(info)


@app.get("/api/styles")
def styles():
    return jsonify([{"key": s.key, "label": s.label, "emoji": s.emoji,
                     "prompt": build_prompt(s.key)} for s in STYLES.values()])


@app.get("/api/history")
def history():
    return jsonify([_public(r) for r in store.recent(int(request.args.get("limit", 24)))])


@app.get("/media/<kind>/<path:name>")
def media(kind: str, name: str):
    folders = {"source": UPLOAD_DIR, "result": RESULT_DIR, "frame": FRAME_DIR, "eink": EINK_DIR}
    folder = folders.get(kind)
    if folder is None:
        return jsonify({"error": "unknown media kind"}), 404
    return send_from_directory(folder, name, max_age=3600)


@app.post("/api/generate")
def generate():
    if not remixer.configured:
        return jsonify({"error": "OPENROUTER_API_KEY is not configured on the server"}), 503
    upload = request.files.get("photo")
    if upload is None or not upload.filename:
        return jsonify({"error": "no photo uploaded"}), 400

    style = request.form.get("style", "comic")
    if style not in STYLES:
        style = random_style()
    extra = (request.form.get("extra") or "")[:400]
    model = (request.form.get("model") or "").strip() or remixer.DEFAULT_MODEL
    dither = request.form.get("dither", "floyd_steinberg")
    if dither not in artframe.DITHERS:
        dither = "floyd_steinberg"
    quantize = request.form.get("quantize", "0").lower() in ("1", "true", "on", "yes")

    gen_id = uuid.uuid4().hex[:12]
    raw_path = UPLOAD_DIR / f"{gen_id}_raw"
    src_path = UPLOAD_DIR / f"{gen_id}_src.jpg"
    upload.save(raw_path)
    try:
        pipeline.normalise(raw_path, src_path)
    except ImageError as exc:
        raw_path.unlink(missing_ok=True)
        return jsonify({"error": f"unsupported image: {exc}"}), 400
    finally:
        raw_path.unlink(missing_ok=True)

    try:
        result = remixer.remix(src_path, style, extra=extra, model=model)
    except RemixError as exc:
        return jsonify({"error": str(exc), "retryable": True}), 502

    result_path = RESULT_DIR / f"{gen_id}.png"
    result_path.write_bytes(result.image_bytes)
    frame_path = FRAME_DIR / f"{gen_id}.jpg"
    eink_path = EINK_DIR / f"{gen_id}.png"
    try:
        pipeline.to_frame(result_path, frame_path, eink_path, dither=dither,
                          quantize_upload=quantize)
        dims = pipeline.probe(frame_path)
    except ImageError as exc:
        return jsonify({"error": f"frame rendering failed: {exc}"}), 500

    store.add(id=gen_id, style=style, model=result.model, extra=extra,
              source_path=str(src_path), result_path=str(result_path),
              frame_path=str(frame_path), eink_path=str(eink_path),
              width=dims.get("width"), height=dims.get("height"),
              quantized=int(quantize),
              cost_usd=result.cost_usd, seconds=result.seconds)
    row = store.get(gen_id) or {}
    payload = _public(row)
    payload.update({"cost_usd": round(result.cost_usd, 4),
                    "seconds": round(result.seconds, 1),
                    "model": result.model})
    return jsonify(payload)


@app.delete("/api/gallery/<gen_id>")
def gallery_delete(gen_id: str):
    """Delete one generation from the local gallery (row + source/remix/frame/EInk files).

    The frame's 10-slot album is untouched: the SwitchBot API only supports
    random/newest/oldest album deletion, never "delete this artwork".
    """
    row = store.get(gen_id)
    if row is None:
        return jsonify({"error": "unknown generation"}), 404
    removed = store.delete(gen_id)
    return jsonify({"ok": True, "id": gen_id, "removed_files": [p.name for p in removed],
                    "remaining": store.stats()["n"]})


@app.post("/api/push/<gen_id>")
def push(gen_id: str):
    row = store.get(gen_id)
    if row is None:
        return jsonify({"error": "unknown generation"}), 404
    client = switchbot()
    if client is None:
        return jsonify({"error": "SwitchBot token/secret not configured "
                                 "(set SWITCHBOT_TOKEN and SWITCHBOT_SECRET)"}), 503
    if not client.device_id:
        try:
            frames = client.art_frames()
        except SwitchBotError as exc:
            return jsonify({"error": f"SwitchBot: {exc}"}), 502
        if len(frames) != 1:
            return jsonify({"error": "several or zero AI Art Frames found; "
                                     "set SWITCHBOT_DEVICE_ID explicitly",
                            "frames": frames}), 409
        client.device_id = frames[0]["deviceId"]
    # Orientation: "auto" replays the verified panel-buffer rotation (FRAME_ROTATE).
    # "none" uploads the landscape composition un-rotated, which is the A/B candidate for the
    # SwitchBot app showing landscape artwork on its side (see README "Orientation").
    body = request.get_json(silent=True) or {}
    orientation = str(body.get("orientation") or request.form.get("orientation") or "auto").lower()
    if orientation not in ("auto", "none", "cw", "ccw"):
        return jsonify({"error": f"unsupported orientation: {orientation}"}), 400
    rotate = None if orientation == "auto" else orientation

    frame_path = Path(row["frame_path"])
    if rotate is not None:
        variant = FRAME_DIR / f"{gen_id}__{orientation}.jpg"
        try:
            pipeline.to_frame(Path(row["result_path"]), variant, None, rotate=rotate)
        except ImageError as exc:
            return jsonify({"error": f"frame rendering failed: {exc}"}), 500
        frame_path = variant

    try:
        result = client.push_image(frame_path)
    except SwitchBotError as exc:
        return jsonify({"error": f"SwitchBot: {exc}"}), 502
    result = {**(result or {}), "orientation": orientation,
              "uploaded_size": pipeline.probe(frame_path)}
    store.mark_pushed(gen_id, result)
    return jsonify({"ok": True, "device_id": client.device_id, "result": result,
                    "orientation": orientation, "pushed_at": time.time()})


@app.get("/api/frame/artwork")
def frame_artwork():
    """Diagnostics: fetch the artwork the frame currently displays from its cloud URL.

    Reports pixel dimensions and the EXIF orientation tag (if any) so we can compare our own
    uploads with AI Studio's artwork when debugging how the app rotates landscape images.
    """
    client = switchbot()
    if client is None:
        return jsonify({"error": "SwitchBot token/secret not configured"}), 503
    import requests
    try:
        status = client.status()
        url = status.get("imageUrl")
        if not url:
            return jsonify({"error": "frame status has no imageUrl"}), 404
        blob = requests.get(url, timeout=40).content
    except (SwitchBotError, requests.RequestException) as exc:
        return jsonify({"error": str(exc)}), 502
    target = MEDIA_DIR / "frame_artwork.jpg"
    target.write_bytes(blob)
    return jsonify({"ok": True, "bytes": len(blob),
                    "dimensions": pipeline.probe(target),
                    "exif_orientation": artframe.jpeg_exif_orientation(target),
                    "url": "/media/frame/frame_artwork.jpg"})


@app.get("/api/frames")
def frames():
    client = switchbot()
    if client is None:
        return jsonify({"error": "SwitchBot token/secret not configured"}), 503
    try:
        return jsonify({"frames": client.art_frames()})
    except SwitchBotError as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/control/<command>")
def control(command: str):
    if command not in ("next", "previous"):
        return jsonify({"error": "unsupported command"}), 400
    client = switchbot()
    if client is None:
        return jsonify({"error": "SwitchBot token/secret not configured"}), 503
    try:
        return jsonify({"ok": True, "result": client.command(command)})
    except SwitchBotError as exc:
        return jsonify({"error": str(exc)}), 502


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "photo is larger than 32 MB"}), 413


if __name__ == "__main__":
    print(f"ArtFrame Studio on http://{HOST}:{PORT} "
          f"(remix={'ready' if remixer.configured else 'no API key'}, "
          f"switchbot={'ready' if switchbot() else 'not configured'})")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
