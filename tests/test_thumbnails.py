import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from milloclips.ai import AI
from milloclips.thumbnails import _game_asset, _reaction_photo, render_thumbnails


class ThumbnailTests(unittest.TestCase):
    def test_concepts_reject_words_not_grounded_in_their_beat_evidence(self):
        ai = object.__new__(AI)
        ai.mock = False
        ai.vod_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ai.vod_dir)
        ai._json = lambda *_args, **_kwargs: {"concepts": [{
            "title": "Dragón decisivo", "headline": "DRAGÓN DECISIVO", "hook": "high-stakes/amazing moment",
            "template": "action", "expression": "shocked", "beat_id": 7, "evidence": "pelea final",
        }]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "Fiddlesticks support gana la pelea final", "reason": "Final fight wins the game"}]
        self.assertEqual(ai.thumbnail_concepts(beats, "Fiddlesticks support wins", []), [])

    def test_concepts_reject_titles_not_grounded_in_their_beat_evidence(self):
        ai = object.__new__(AI)
        ai.mock = False
        ai.vod_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ai.vod_dir)
        ai._json = lambda *_args, **_kwargs: {"concepts": [{
            "title": "Fiddlesticks decisivo", "headline": "MOMENTO FIDDLE", "hook": "high-stakes/amazing moment",
            "template": "action", "expression": "shocked", "beat_id": 7, "evidence": "Fiddlesticks support",
        }]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "Fiddlesticks support gana la pelea final", "reason": "Final fight wins the game"}]
        self.assertEqual(ai.thumbnail_concepts(beats, "Fiddlesticks support wins", []), [])

    def test_concepts_reject_hypotheticals_as_performance(self):
        ai = object.__new__(AI)
        ai.mock = False
        ai.vod_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ai.vod_dir)
        ai._json = lambda *_args, **_kwargs: {"concepts": [{
            "title": "Podría ganar", "headline": "PODRÍA GANAR", "hook": "extremely good performance",
            "template": "action", "expression": "hyped", "beat_id": 7, "evidence": "podría ganar",
        }]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "podría ganar la pelea", "reason": "Potential fight"}]
        self.assertEqual(ai.thumbnail_concepts(beats, "Potential fight", []), [])

    def test_concepts_reject_copy_without_elmiillor_as_the_subject(self):
        ai = object.__new__(AI)
        ai.mock = False
        ai.vod_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ai.vod_dir)
        ai._json = lambda *_args, **_kwargs: {"concepts": [{
            "title": "Tristana W", "headline": "TRISTANA RESET W", "hook": "bold claim/strategy",
            "template": "action", "expression": "hyped", "beat_id": 7, "evidence": "Tristana reset W",
        }]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "Tristana reset W", "reason": "Visible reset"}]
        self.assertEqual(ai.thumbnail_concepts(beats, "Tristana reset", []), [])

    def test_three_generic_variants_use_only_final_edit_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vod = root / "vod"
            frames = vod / "frames" / "selected-game"
            frames.mkdir(parents=True)
            faces = root / "faces"
            faces.mkdir()
            (vod / "long-plan.json").write_text(json.dumps({"game_id": "game-002", "selected_ids": [1, 3], "story": "A strange pick wins."}))
            (vod / "beats.json").write_text(json.dumps({"target_beat_seconds": 15, "beats": [
                {"id": 0, "start": 0, "importance": 1, "type": "FARMING"},
                {"id": 1, "start": 15, "importance": 5, "type": "COMBAT"},
                {"id": 2, "start": 30, "importance": 1, "type": "FARMING"},
                {"id": 3, "start": 45, "importance": 4, "type": "REACTION"}]}))
            for number in range(12):
                Image.new("RGB", (96, 54), "#543267").save(frames / f"frame_{number:06d}.jpg")
            for expression in ("shocked", "hold-on", "hyped", "smiling"):
                Image.new("RGBA", (80, 80), "red").save(faces / f"{expression}.png")
            sheet = render_thumbnails(vod, faces, ai=AI(vod, mock=True))
            render_thumbnails(vod, faces, ai=AI(vod, mock=True))
            with Image.open(sheet) as contact:
                self.assertEqual(contact.size, (1480, 350))
            self.assertEqual(sheet.parent, vod / "renders" / "thumbnails")
            rows = json.loads((sheet.parent / "variants.json").read_text())
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["beat_id"] in {1, 3} and Path(row["frame"]).exists() for row in rows))
            self.assertTrue((vod / "thumbnail-brief.json").exists())
            self.assertTrue((vod / "renders" / "thumbnail.png").exists())
            for row in rows:
                with Image.open(row["render"]) as rendered:
                    self.assertEqual(rendered.size, (1280, 720))
            with Image.open(rows[1]["render"]) as gameplay:
                self.assertNotEqual(gameplay.getpixel((500, 400)), gameplay.getpixel((1050, 400)))

    def test_reaction_selection_uses_named_expression_and_supported_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            faces = Path(tmp)
            Image.new("RGB", (8, 8)).save(faces / "shocked.webp")
            self.assertEqual(_reaction_photo(faces, "shocked"), faces / "shocked.webp")
            self.assertIsNone(_reaction_photo(faces, "smiling"))

    def test_rune_asset_is_fetched_only_when_requested_and_then_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            catalog = json.dumps([{"slots": [{"runes": [{"id": 8112,
                "icon": "perk-images/Styles/Domination/Electrocute/Electrocute.png"}]}]}]).encode()
            image = io.BytesIO()
            Image.new("RGB", (64, 64), "red").save(image, "PNG")
            with patch("milloclips.thumbnails.urlopen", side_effect=[io.BytesIO(catalog), io.BytesIO(image.getvalue())]) as fetch:
                icon = _game_asset(output, "rune", "8112")
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(_game_asset(output, "rune", "8112"), icon)
                self.assertEqual(fetch.call_count, 2)
            with Image.open(icon) as saved:
                self.assertEqual(saved.size, (64, 64))


if __name__ == "__main__":
    unittest.main()
