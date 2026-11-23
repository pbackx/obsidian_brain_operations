# ArtFrame Studio

Self-hosted, subscription-free replacement for the SwitchBot **AI Studio** plan on a
first-generation **AI Art Frame** (W8402000 series, 6-colour E Ink Spectra 6).

Flow: **photo → AI style remix → crop/dither to the panel's six inks → push over the SwitchBot
OpenAPI** (no public URL, no Cloud subscription).

## Why this works without AI Studio

The SwitchBot v1.1 OpenAPI already exposes everything the subscription uses for the one feature
that matters (`uploadImage`):

| Command | Parameter | Effect |
| --- | --- | --- |
| `uploadImage` | `{"imageBase64": "data:image/jpeg;base64,..."}` or `{"imageUrl": "https://..."}` | Adds the image to the frame and displays it |
| `next` / `previous` | `default` | Cycle through the stored images |

Because base64 is accepted, the service never needs a publicly reachable URL — the whole
pipeline can stay inside the LAN. Home Assistant's official SwitchBot integration does **not**
support the AI Art Frame (no `SupportedModels` entry), so this service talks to the cloud API
itself and HA calls it via `rest_command` (see below).

## Components

| File | Purpose |
| --- | --- |
| `artframe.py` | SwitchBot v1.1 HMAC client, style presets + prompt builder, ffmpeg E Ink pipeline, SQLite gallery (with delete) |
| `app.py` | Flask UI/JSON API (Flask Vibe: server-rendered page, vanilla ES modules, no build step) |
| `templates/index.html`, `static/` | Drag-and-drop UI: remix controls, Result panel (preview + push), gallery with per-item delete |
| `tests/test_core.py` | Unit tests (signing, prompts, palette, crop, store) |

Only `python3`, `requests`, `flask` and a static `ffmpeg` binary are required — no Pillow, no
numpy, no model weights. Idle footprint is a few MB RSS, so it fits a tiny LXC container.

## Install

```bash
sudo apt install -y python3-flask ffmpeg            # or reuse the static ffmpeg in ~/.local/bin
cp artframe.env.example artframe.env               # then fill in the values
python3 app.py                                     # http://<host>:8095
```

`artframe.env` (git-ignored, never committed):

```
OPENROUTER_API_KEY=sk-or-v1-...
SWITCHBOT_TOKEN=...            # SwitchBot app > Profile > About > tap version 10x > Developer Options
SWITCHBOT_SECRET=...
SWITCHBOT_DEVICE_ID=...        # optional; auto-detected when exactly one Art Frame is on the account
FRAME_WIDTH=1200
FRAME_HEIGHT=1600
```

## Deploy as a tiny LXC (CT 110 `artframe.home.arpa`)

Live since 2026-11-21: unprivileged Ubuntu 24.04 container on `local-nvme` (4 GB disk,
512 MB RAM), `python3-flask python3-requests python3-gunicorn ffmpeg`, app in
`/opt/artframe_studio`, secrets in `/etc/artframe/artframe.env` (mode 600, owned by the
`artframe` system user), data in `/var/lib/artframe`.

```bash
# on the Proxmox host / from a machine with the container key
adduser --system --group --home /var/lib/artframe --shell /usr/sbin/nologin artframe
install -d /opt/artframe_studio /etc/artframe
# copy the app, then:
cp deploy/artframe-studio.service /etc/systemd/system/
install -m 600 -o artframe -g artframe /path/to/artframe.env /etc/artframe/artframe.env
systemctl daemon-reload && systemctl enable --now artframe-studio
curl -s localhost:8095/api/health
```

The unit runs `python3 -m gunicorn` (1 worker, 4 threads) with `MemoryMax=400M` and
`ProtectSystem=full` + `ProtectHome=read-only` (only `/var/lib/artframe` is writable).

## Frame geometry: the 7.3" model hung in landscape

Measured live on the 7.3" panel (model code `W1130000`): the pixel buffer is **480 x 800
portrait** and the album holds **max 10 images**. `FRAME_WIDTH=800 FRAME_HEIGHT=480` describes
the *composition* as it hangs on the wall; the 7.3" firmware accepts that un-rotated landscape
JPEG directly, so `FRAME_ROTATE=none` is the default.

### Orientation: resolved 2026-11-23

History: SwitchBot's own AI Studio uploads landscape artwork pre-rotated 90 deg clockwise
(480 x 800), and the first version of this service copied that. The wall looked right, but the
phone app showed the artwork sideways, because the app renders the **raw cloud copy** of the
upload (no EXIF tag survives: 480 x 800, re-encoded 139 KB -> 48 KB, measured with
`GET /api/frame/artwork`).

The A/B push (`orientation: "none"`, an un-rotated 800 x 480 upload) was tested on the real
panel and reads **upright both on the wall and in the app**, so that is now the default
(`FRAME_ROTATE=none`). Consequences:

* The upload is the composition itself: no rotation, no double resampling.
* The gallery stores (and the Studio shows) the upright landscape composition only. Any
  rotation is applied to a throw-away copy at push time (`<id>__upload_<rot>.jpg`), so a
  sideways preview can no longer appear in the UI.
* `POST /api/push/<gen_id>` still accepts `{"orientation": "auto"|"none"|"cw"|"ccw"}` for
  per-push A/B tests; `auto` follows `FRAME_ROTATE`. Only go back to `cw` if a different frame
  model genuinely needs the legacy pre-rotated buffer.

The UI now reports push/deletion feedback inside the **Result** panel (where the buttons are)
instead of the Generate panel, and the *Frame artwork (upright)* tab shows exactly the
composition that gets pushed.

## Home Assistant integration

Add REST commands and a dashboard button (no custom component needed):

```yaml
# configuration.yaml
rest_command:
  artframe_comic:
    url: "http://localhost:8095/api/generate"
    method: post
```

Simplest reliable variant: an `input_select` with the styles, a `script` that posts the selected
photo (via `curl -F`) and then a Webpage/iframe card that shows the studio itself.
See the task dossier for the full YAML.

## Cost

`google/gemini-2.5-flash-image` via OpenRouter: ~**$0.039 per remix**, 5–15 s,
~1.2 MP output. Two remixes a month ≈ €0.07 versus a monthly AI Studio subscription.

## Notes / limits

* A full album rejects every `uploadImage` with `code=7301 image count limit reached`.
  `ARTFRAME_EVICT_FULL=1` (default) then issues `deleteImage oldest` and retries, because the
  frame has no self-cleaning. The currently displayed image is never deleted by that selector.

* `next`/`previous` are documented; the frame keeps a small local image list, so pushing a new
  image effectively replaces the displayed artwork.
* E Ink Spectra 6 palette values in `SPECTRA6` are approximations — tune them after a visual
  test on the panel (they only affect the preview JPEG that is uploaded).
* The 3:4 geometry defaults to the 13.3" model; set `FRAME_WIDTH`/`FRAME_HEIGHT` for 7.3"/31.5".
