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
    try:
        result = client.push_image(Path(row["frame_path"]))
    except SwitchBotError as exc:
        return jsonify({"error": f"SwitchBot: {exc}"}), 502
    store.mark_pushed(gen_id, result)
    return jsonify({"ok": True, "device_id": client.device_id, "result": result,
                    "pushed_at": time.time()})


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
