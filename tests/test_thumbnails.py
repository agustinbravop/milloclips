import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from milloclips.thumbnails import REACTIONS, VARIANTS, _game_asset, _reaction_photo, render_thumbnails


class ThumbnailTests(unittest.TestCase):
    def test_six_variants_use_cached_icons_and_actual_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vod = root / "vod"
            frames = vod / "frames" / "selected-game"
            frames.mkdir(parents=True)
            faces = root / "faces"
            faces.mkdir()
            (vod / "manifest.json").write_text(json.dumps({"metadata": {
                "input": "https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e"}}))
            (vod / "long-plan.json").write_text(json.dumps({"game_id": "game-002", "selected_ids": [-1]}))
            (vod / "beats.json").write_text(json.dumps({"beats": [{"id": -1, "text": "Porque existe un ítem llamado Zeke"}]}))
            item = vod / "renders" / "thumbnails" / "asset-item-3050.png"
            item.parent.mkdir(parents=True)
            Image.new("RGB", (64, 64), "#ffc33b").save(item)
            Image.new("RGB", (64, 64), "#76ccff").save(item.parent / "asset-champion-Fiddlesticks.png")
            Image.new("RGB", (64, 64), "red").save(item.parent / "raw-01-obsolete.png")
            Image.new("RGB", (64, 64), "red").save(item.parent / "01-comeback.png")
            for number in {185, *(variant[1] for variant in VARIANTS)}:
                Image.new("RGB", (96, 54), "#543267").save(frames / f"frame_{number:06d}.jpg")
            for expression in set(REACTIONS):
                Image.new("RGBA", (80, 80), "red").save(faces / f"{expression}.png")
            with patch("milloclips.thumbnails.urlopen", side_effect=AssertionError("Icons should be cached")):
                sheet = render_thumbnails(vod, faces)
                render_thumbnails(vod, faces)
            with Image.open(sheet) as contact:
                self.assertEqual(contact.size, (1480, 690))
            self.assertEqual(sheet.parent, vod / "renders" / "thumbnails")
            self.assertFalse((sheet.parent / "raw-01-obsolete.png").exists())
            self.assertFalse((sheet.parent / "01-comeback.png").exists())
            rows = json.loads((sheet.parent / "variants.json").read_text())
            self.assertEqual(len(rows), 6)
            self.assertEqual([Path(row["reaction_photo"]).stem for row in rows], list(REACTIONS))
            self.assertEqual(len({row["title"] for row in rows}), 6)
            self.assertEqual([row["item"] for row in rows],
                             [None, "Zeke's Convergence", "Zeke's Convergence",
                              "Zeke's Convergence", None, "Zeke's Convergence"])
            self.assertTrue(all("art" not in row and Path(row["frame"]).exists() for row in rows))
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
