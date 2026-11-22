"""Unit tests for ArtFrame Studio core.  Run:  python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import base64
import hashlib
import hmac
import subprocess
import sys
import tempfile
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artframe  # noqa: E402
from artframe import (EinkPipeline, SPECTRA6, Store, SwitchBotClient, build_prompt,  # noqa: E402
                      data_uri)

TOKEN = "TESTTOKEN"
SECRET = "TESTSECRET"


class SigningTests(unittest.TestCase):
    def test_signature_matches_reference_hmac(self):
        client = SwitchBotClient(TOKEN, SECRET)
        headers = client.headers(timestamp_ms=1661927531000, nonce="fixed-nonce")
        expected = base64.b64encode(
            hmac.new(SECRET.encode(), f"{TOKEN}1661927531000fixed-nonce".encode(),
                     hashlib.sha256).digest()
        ).decode()
        self.assertEqual(headers["sign"], expected)
        self.assertEqual(headers["Authorization"], TOKEN)
        self.assertEqual(headers["t"], "1661927531000")
        self.assertEqual(headers["nonce"], "fixed-nonce")
        self.assertEqual(len(base64.b64decode(headers["sign"])), 32)

    def test_uppercase_sign_toggle(self):
        lower = SwitchBotClient(TOKEN, SECRET).headers(1, "n")["sign"]
        upper = SwitchBotClient(TOKEN, SECRET, uppercase_sign=True).headers(1, "n")["sign"]
        self.assertEqual(upper, lower.upper())

    def test_headers_are_unique_per_call(self):
        client = SwitchBotClient(TOKEN, SECRET)
        a, b = client.headers(), client.headers()
        self.assertNotEqual(a["nonce"], b["nonce"])
        self.assertNotEqual(a["sign"], b["sign"])


class PromptTests(unittest.TestCase):
    def test_prompt_contains_style_and_constraints(self):
        prompt = build_prompt("cyberpunk")
        self.assertIn("cyberpunk", prompt)
        self.assertIn("six-colour E Ink Spectra 6", prompt)
        self.assertIn("Do not add any text", prompt)

    def test_extra_direction_is_appended(self):
        self.assertIn("add a bicycle", build_prompt("manga", "add a bicycle"))

    def test_unknown_style_falls_back(self):
        self.assertEqual(build_prompt("does-not-exist"), build_prompt("comic"))

    def test_every_style_has_prompt(self):
        for key, style in artframe.STYLES.items():
            self.assertTrue(style.prompt, key)
            self.assertTrue(style.label, key)


class PaletteAndPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.src = cls.dir / "src.png"
        subprocess.run([artframe.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=800x600:duration=1", "-frames:v", "1",
                        str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_palette_is_256_entries(self):
        pal = artframe.palette_png(self.dir / "pal.png")
        dims = EinkPipeline.probe(pal)
        self.assertEqual((dims["width"], dims["height"]), (16, 16))
        self.assertEqual(len(SPECTRA6), 6)

    def test_frame_output_is_panel_geometry(self):
        pipe = EinkPipeline(width=150, height=200, palette_dir=self.dir, rotate="none")
        out = self.dir / "frame.jpg"
        preview = self.dir / "eink.png"
        pipe.to_frame(self.src, out, preview)
        self.assertEqual(EinkPipeline.probe(out), {"width": 150, "height": 200})
        self.assertEqual(EinkPipeline.probe(preview), {"width": 150, "height": 200})

    def test_no_dither_path(self):
        pipe = EinkPipeline(width=120, height=160, palette_dir=self.dir, rotate="none")
        out = self.dir / "plain.jpg"
        pipe.to_frame(self.src, out, None, dither="none")
        self.assertEqual(EinkPipeline.probe(out)["height"], 160)

    def test_unknown_dither_rejected(self):
        pipe = EinkPipeline(width=60, height=80, palette_dir=self.dir, rotate="none")
        with self.assertRaises(ValueError):
            pipe.to_frame(self.src, self.dir / "x.jpg", None, dither="atkinson")

    def test_landscape_rotation_writes_portrait_buffer(self):
        """A landscape 7.3" composition is uploaded pre-rotated to the 480x800 panel buffer."""
        pipe = EinkPipeline(width=200, height=120, palette_dir=self.dir, rotate="cw")
        out = self.dir / "landscape_upload.jpg"
        preview = self.dir / "landscape_eink.png"
        pipe.to_frame(self.src, out, preview)
        self.assertEqual(EinkPipeline.probe(out), {"width": 120, "height": 200})
        self.assertEqual(EinkPipeline.probe(preview), {"width": 200, "height": 120})

    def test_landscape_rotation_with_quantized_upload(self):
        pipe = EinkPipeline(width=200, height=120, palette_dir=self.dir, rotate="ccw")
        out = self.dir / "landscape_quantized.jpg"
        pipe.to_frame(self.src, out, None, quantize_upload=True)
        self.assertEqual(EinkPipeline.probe(out), {"width": 120, "height": 200})

    def test_rotation_filter_values(self):
        self.assertEqual(artframe.rotation_filter("none"), "")
        self.assertEqual(artframe.rotation_filter("cw"), ",transpose=1")
        self.assertEqual(artframe.rotation_filter("ccw"), ",transpose=2")
        with self.assertRaises(ValueError):
            artframe.rotation_filter("flip")

    def test_data_uri(self):
        uri = data_uri(self.src, "image/png")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertGreater(len(uri), 100)


class StoreTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "g.sqlite3")
            gen_id = store.add(style="comic", model="m", cost_usd=0.039, seconds=7.8,
                               frame_path="/tmp/x.jpg")
            row = store.get(gen_id)
            self.assertEqual(row["style"], "comic")
            self.assertIsNone(row["pushed_at"])
            store.mark_pushed(gen_id, {"commandId": "CMD1"})
            self.assertIsNotNone(store.get(gen_id)["pushed_at"])
            self.assertEqual(store.stats()["n"], 1)
            self.assertEqual(len(store.recent()), 1)


class ClientConfigTests(unittest.TestCase):
    def test_from_env_returns_none_without_credentials(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in
                 ("SWITCHBOT_TOKEN", "SWITCHBOT_SECRET", "SWITCHBOT_OPEN_TOKEN",
                  "SWITCHBOT_SECRET_KEY")}
        try:
            self.assertIsNone(SwitchBotClient.from_env())
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_from_env_reads_credentials(self):
        import os
        os.environ.update({"SWITCHBOT_TOKEN": TOKEN, "SWITCHBOT_SECRET": SECRET,
                           "SWITCHBOT_DEVICE_ID": "ABC123"})
        client = SwitchBotClient.from_env()
        self.assertEqual(client.device_id, "ABC123")
        os.environ.pop("SWITCHBOT_DEVICE_ID")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EvictionTests(unittest.TestCase):
    def test_full_album_evicts_oldest_then_retries(self):
        from unittest import mock

        client = SwitchBotClient(TOKEN, SECRET, "DEV")
        calls = []

        def fake_request(method, path, payload=None):
            calls.append(payload)
            uploads = [c for c in calls if c["command"] == "uploadImage"]
            if payload["command"] == "uploadImage" and len(uploads) == 1:
                raise artframe.SwitchBotError(
                    "rpc error: code=7301 message=image count limit reached: 10",
                    190, {"statusCode": 190, "message": "code=7301 image count limit reached"})
            return {"ok": True}

        with mock.patch.object(client, "_request", side_effect=fake_request), \
                mock.patch.object(artframe.time, "sleep", lambda *_: None):
            result = client.push_image(Path(__file__), evict_on_full=True)

        self.assertEqual([c["command"] for c in calls],
                         ["uploadImage", "deleteImage", "uploadImage"])
        self.assertEqual(calls[1]["parameter"], "oldest")
        self.assertTrue(result["ok"])

    def test_full_album_without_eviction_raises(self):
        from unittest import mock

        client = SwitchBotClient(TOKEN, SECRET, "DEV")
        with mock.patch.object(client, "_request",
                               side_effect=artframe.SwitchBotError(
                                   "code=7301 image count limit reached", 190, {})), \
                mock.patch.object(artframe.time, "sleep", lambda *_: None):
            with self.assertRaises(artframe.SwitchBotError):
                client.push_image(Path(__file__), evict_on_full=False)

    def test_delete_image_validates_selector(self):
        with self.assertRaises(ValueError):
            SwitchBotClient(TOKEN, SECRET).delete_image("middle")


class StyleStrengthTests(unittest.TestCase):
    def test_prompts_force_a_medium_change(self):
        for key, style in artframe.STYLES.items():
            prompt = build_prompt(key)
            self.assertIn("Completely repaint", prompt, key)
            self.assertIn("must NOT look like a photograph", prompt, key)
            # each preset must carry concrete, medium-specific vocabulary
            self.assertGreater(len(style.prompt.split()), 25, f"{key} prompt is too thin")

    def test_styles_are_distinct(self):
        prompts = {build_prompt(key) for key in artframe.STYLES}
        self.assertEqual(len(prompts), len(artframe.STYLES))

    def test_painterly_styles_keep_their_texture(self):
        # the old shared suffix flattened everything into flat colour; painters need their medium
        for key in ("watercolour", "oilpainting", "pencil"):
            self.assertIn("texture" if key != "pencil" else "paper", build_prompt(key).lower(), key)


class PerCallRotationTests(unittest.TestCase):
    def test_rotate_override_does_not_mutate_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            src = d / "src.png"
            subprocess.run([artframe.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=200x120:duration=1", "-frames:v", "1",
                            str(src)], check=True)
            pipe = EinkPipeline(width=200, height=120, palette_dir=d, rotate="cw")
            out = d / "override.jpg"
            pipe.to_frame(src, out, None, rotate="none")
            self.assertEqual(EinkPipeline.probe(out), {"width": 200, "height": 120})
            self.assertEqual(pipe.rotate, "cw")
            with self.assertRaises(ValueError):
                pipe.to_frame(src, d / "bad.jpg", None, rotate="flip")


class JpegExifTests(unittest.TestCase):
    def test_orientation_is_none_without_exif(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            img = d / "plain.jpg"
            subprocess.run([artframe.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=64x48:duration=1", "-frames:v", "1",
                            str(img)], check=True)
            self.assertIsNone(artframe.jpeg_exif_orientation(img))
            self.assertIsNone(artframe.jpeg_exif_orientation(Path(__file__)))

    def test_orientation_is_read_from_app1_segment(self):
        # Minimal EXIF APP1 with a single Orientation (=8) entry, big-endian TIFF.
        tiff = (b"MM\x00\x2a" + (8).to_bytes(4, "big") + (1).to_bytes(2, "big")
                + (0x0112).to_bytes(2, "big") + (3).to_bytes(2, "big")
                + (1).to_bytes(4, "big") + (8).to_bytes(2, "big") + b"\x00\x00"
                + (0).to_bytes(4, "big"))
        payload = b"Exif\x00\x00" + tiff
        seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
        jpeg = b"\xff\xd8" + seg + b"\xff\xda\x00\x02\xff\xd9"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "exif.jpg"
            p.write_bytes(jpeg)
            self.assertEqual(artframe.jpeg_exif_orientation(p), 8)


class GalleryDeleteTests(unittest.TestCase):
    def test_delete_removes_row_and_media_but_not_outside_media_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            media.mkdir()
            inside = media / "keep.jpg"
            inside.write_bytes(b"x")
            outside = Path(tmp) / "precious.txt"
            outside.write_text("do not delete")
            os.environ["ARTFRAME_DATA_DIR"] = tmp
            try:
                store = Store(Path(tmp) / "g.sqlite3")
                gen_id = store.add(style="manga", model="m", frame_path=str(inside),
                                   source_path=str(outside))
                removed = store.delete(gen_id)
                self.assertEqual([p.name for p in removed], ["keep.jpg"])
                self.assertIsNone(store.get(gen_id))
                self.assertFalse(inside.exists())
                self.assertTrue(outside.exists(), "files outside the media tree must survive")
                self.assertEqual(store.delete(gen_id), [])
            finally:
                os.environ.pop("ARTFRAME_DATA_DIR", None)
