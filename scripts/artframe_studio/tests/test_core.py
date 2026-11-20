"""Unit tests for ArtFrame Studio core.  Run:  python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import base64
import hashlib
import hmac
import subprocess
import sys
import tempfile
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
        pipe = EinkPipeline(width=150, height=200, palette_dir=self.dir)
        out = self.dir / "frame.jpg"
        preview = self.dir / "eink.png"
        pipe.to_frame(self.src, out, preview)
        self.assertEqual(EinkPipeline.probe(out), {"width": 150, "height": 200})
        self.assertEqual(EinkPipeline.probe(preview), {"width": 150, "height": 200})

    def test_no_dither_path(self):
        pipe = EinkPipeline(width=120, height=160, palette_dir=self.dir)
        out = self.dir / "plain.jpg"
        pipe.to_frame(self.src, out, None, dither="none")
        self.assertEqual(EinkPipeline.probe(out)["height"], 160)

    def test_unknown_dither_rejected(self):
        pipe = EinkPipeline(width=60, height=80, palette_dir=self.dir)
        with self.assertRaises(ValueError):
            pipe.to_frame(self.src, self.dir / "x.jpg", None, dither="atkinson")

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
