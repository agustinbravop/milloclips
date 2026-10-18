import io
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from PIL import Image, ImageDraw

from milloclips.ai import AI, thumbnail_copy, thumbnail_preferences
from milloclips.cli import main
from milloclips.thumbnails import _contact_sheet, _game_asset, _generated_layout, _publish_candidates, _reaction, _reaction_photo, prepare_thumbnail_references, render_thumbnails, revise_thumbnail
from milloclips.thumbnail_art import MODEL, generate_artwork, plan_artwork


class ThumbnailTests(unittest.TestCase):
    def test_reactions_face_inward_and_old_orientation_can_be_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "reaction.png"
            source = Image.new("RGBA", (100, 100), "red")
            source.paste("blue", (50, 0, 100, 100))
            source.save(photo)
            original, inward = [Image.new("RGB", (1280, 720)) for _ in range(2)]
            _reaction(original, photo, "right", mirrored=True)
            _reaction(inward, photo, "right")
            self.assertEqual(original.getpixel((730, 300)), (0, 0, 255))
            self.assertEqual(inward.getpixel((730, 300)), (255, 0, 0))
            self.assertEqual(original.getpixel((1220, 300)), (255, 0, 0))
            self.assertEqual(inward.getpixel((1220, 300)), (0, 0, 255))
            left = Image.new("RGB", (1280, 720))
            _reaction(left, photo, "left")
            self.assertEqual(left.getpixel((60, 300)), (0, 0, 255))
            self.assertEqual(left.getpixel((550, 300)), (255, 0, 0))

    def test_reaction_alpha_is_not_premultiplied_into_dark_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "reaction.png"
            Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(photo)
            layer = Image.new("RGBA", (1280, 720))
            _reaction(layer, photo)
            r, _g, _b, alpha = layer.getpixel((900, 300))
            self.assertEqual(r, 255)
            self.assertTrue(0 < alpha < 255)

    def test_revision_restores_photo_pane_and_survives_unchanged_rerenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            vod = Path(tmp)
            output = vod / "renders" / "thumbnails"
            output.mkdir(parents=True)
            photo, source, mask, template, artwork = [output / name for name in ("shocked.png", "source.png", "mask.png", "layout.png", "art.png")]
            Image.new("RGBA", (100, 100), "red").save(photo)
            Image.new("RGB", (1280, 720), "gray").save(source)
            Image.new("RGBA", (1280, 720)).save(mask)
            Image.new("RGBA", (1280, 720), "red").save(template)
            Image.new("RGB", (1536, 864), "blue").save(artwork)
            rows = [{"number": number, "title": "Mi partida", "headline": "LOCURA", "expression": "shocked", "render": str(source),
                     "artwork": str(artwork), "reaction_photo": str(photo), "template": str(template), "mask": str(mask),
                     "exact_assets": [], "direction": {"subject_side": "left", "asset_positions": []}} for number in (1, 2, 3)]
            (output / "variants.json").write_text(json.dumps(rows))
            ai = AI(vod, mock=True)
            with patch("milloclips.thumbnails.generate_artwork", return_value=artwork):
                revise_thumbnail(vod, 1, "Fix the headline", ai, headline="MI LOCURA")
            current = json.loads((output / "variants.json").read_text())
            self.assertEqual(current[0]["headline"], "MI LOCURA")
            self.assertNotEqual(current[0]["render"], str(source))
            with Image.open(current[0]["render"]) as final:
                self.assertEqual(final.getpixel((20, 20)), (255, 0, 0))
                self.assertEqual(final.getpixel((1000, 20)), (0, 0, 255))
            _publish_candidates(vod, rows)
            self.assertEqual(json.loads((output / "variants.json").read_text())[0]["render"], current[0]["render"])

    def test_sunburst_cache_budget_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template, mask = root / "template.png", root / "mask.png"
            Image.new("RGB", (1280, 720), "red").save(template)
            Image.new("RGBA", (1280, 720), (0, 0, 0, 0)).save(mask)
            encoded = io.BytesIO()
            Image.new("RGB", (1536, 864), "blue").save(encoded, "PNG")
            usage = {"output_tokens": 5000, "input_tokens_details": {"text_tokens": 100, "image_tokens": 1000}}
            response = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: usage), created=1,
                                       data=[SimpleNamespace(b64_json=base64.b64encode(encoded.getvalue()).decode(), revised_prompt=None)])
            client = MagicMock()
            client.with_options.return_value = client
            client.images.edit.return_value = response
            ai = SimpleNamespace(client=client, mock=False, vod_dir=root)
            artwork = generate_artwork(ai, root, "Make a thumbnail", [template], mask)
            self.assertEqual(generate_artwork(ai, root, "Make a thumbnail", [template], mask), artwork)
            self.assertEqual(client.images.edit.call_count, 1)
            self.assertEqual(client.images.edit.call_args.kwargs["model"], MODEL)
            self.assertNotIn("input_fidelity", client.images.edit.call_args.kwargs)
            self.assertEqual(client.with_options.call_args.kwargs["max_retries"], 0)
            ledger = json.loads((root / "image-usage.json").read_text())
            self.assertAlmostEqual(ledger["requests"][0]["cost_usd"], .1585)
            self.assertFalse(ledger["requests"][0]["estimated"])
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                generate_artwork(ai, root, "A different thumbnail", [template], mask, budget=.2)
            self.assertEqual(client.images.edit.call_count, 1)

    def test_baked_artwork_is_not_covered_by_a_second_photo_or_icon_paste(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo, frame, icon, artwork = [root / name for name in ("shocked.png", "frame.jpg", "icon.png", "art.png")]
            Image.new("RGBA", (100, 100), "red").save(photo)
            Image.new("RGB", (960, 540), "gray").save(frame)
            Image.new("RGBA", (64, 64), "green").save(icon)
            Image.new("RGB", (1536, 864), "blue").save(artwork)
            concept = {"title": "Mi partida", "headline": "QUÉ LOCURA", "expression": "shocked", "champion": "Fiddlesticks"}
            direction = {"subject_side": "left", "scene": "Dramatic Rift scene", "asset_positions": [{"x": 450, "y": 490, "size": 140}]}
            ai = AI(root, mock=True)
            references = [{"id": str(index), "path": str(frame)} for index in range(3)]
            with patch("milloclips.thumbnails._game_asset", return_value=frame), patch("milloclips.thumbnails.plan_artwork", return_value=direction) as plan, patch("milloclips.thumbnails.generate_artwork", return_value=artwork) as generate:
                canvas, metadata = _generated_layout(ai, root, frame, concept, photo, [icon], references, 5)
            self.assertEqual(plan.call_args.kwargs["subject_reference"], frame)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(len(generate.call_args.args[3]), 5)
            self.assertEqual(generate.call_args.args[3][2], frame)
            self.assertEqual(generate.call_args.args[3][-1], photo)
            self.assertEqual(metadata["reference_ids"], [])
            self.assertLess(len(generate.call_args.args[2]), 2200)
            self.assertIn("headline ON TOP", generate.call_args.args[2])
            self.assertEqual(metadata["composition_mode"], "baked-photo")
            self.assertTrue(metadata["reaction_mirrored"])
            self.assertEqual(canvas.getpixel((200, 300)), (0, 0, 255))
            self.assertEqual(canvas.getpixel((500, 550)), (0, 0, 255))
            self.assertEqual(canvas.getpixel((1000, 300)), (0, 0, 255))
            self.assertEqual(canvas.getpixel((613, 300)), (0, 0, 255))
            with Image.open(metadata["mask"]) as protection:
                self.assertEqual(protection.getpixel((200, 300))[3], 0)
                self.assertEqual(protection.getpixel((1000, 300))[3], 0)
            with Image.open(metadata["template"]) as template:
                self.assertEqual(template.getpixel((200, 300))[:3], (255, 0, 0))

    def test_baked_revision_keeps_generated_text_over_the_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            vod = Path(tmp)
            output = vod / "renders" / "thumbnails"
            output.mkdir(parents=True)
            photo, source, artwork = [output / name for name in ("smiling.png", "source.png", "art.png")]
            Image.new("RGBA", (100, 100), "red").save(photo)
            Image.new("RGB", (1280, 720), "gray").save(source)
            Image.new("RGB", (1536, 864), "blue").save(artwork)
            rows = [{"number": number, "title": "MI PARTIDA", "headline": "UDYR AP", "render": str(source),
                     "artwork": str(artwork), "reaction_photo": str(photo), "composition_mode": "baked-photo",
                     "reaction_mirrored": False, "exact_assets": [],
                     "direction": {"subject_side": "right", "asset_positions": []}} for number in (1, 2, 3)]
            (output / "variants.json").write_text(json.dumps(rows))
            with patch("milloclips.thumbnails.generate_artwork", return_value=artwork) as generate:
                revise_thumbnail(vod, 1, "Keep the headline above the torso", AI(vod, mock=True))
            current = json.loads((output / "variants.json").read_text())
            with Image.open(current[0]["render"]) as final:
                self.assertEqual(final.getpixel((900, 500)), (0, 0, 255))
            with Image.open(generate.call_args.args[4]) as mask:
                self.assertIsNone(mask.getchannel("A").getbbox())

    def test_direction_rejects_assets_over_the_face(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "frame.png"
            Image.new("RGB", (100, 100)).save(image)
            ai = AI(root, mock=True)
            ai._json = lambda *_args, **_kwargs: {"subject_side": "left", "scene": "Rift", "asset_positions": [{"x": 20, "y": 20, "size": 100}]}
            with self.assertRaisesRegex(ValueError, "obscure the reaction"):
                plan_artwork(ai, {}, [], image, image, [image], "", root)
            ai._json = lambda *_args, **_kwargs: {"subject_side": "left", "scene": "Rift"}
            self.assertEqual(plan_artwork(ai, {}, [], image, image, [], "", root)["asset_positions"], [])

    def test_reviewed_direction_reuses_raw_call_and_validates_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "frame.png"
            Image.new("RGB", (100, 100)).save(image)
            ai = AI(root, mock=True)
            raw = {"subject_side": "right", "scene": "Rift", "asset_positions": [{"x": 590, "y": 550, "size": 160}]}
            ai._json = lambda *_args, **_kwargs: raw
            with self.assertRaisesRegex(ValueError, "outside its canvas"):
                plan_artwork(ai, {}, [], image, image, [image], "", root)
            raw_path = next(root.glob("direction-*.raw.json"))
            reviewed_path = raw_path.with_name(raw_path.name.replace(".raw.json", ".reviewed.json"))
            corrected = {**raw, "asset_positions": [{"x": 590, "y": 548, "size": 160}]}
            reviewed_path.write_text(json.dumps(corrected))
            with patch.object(ai, "_json", side_effect=AssertionError("Must reuse cached direction")):
                self.assertEqual(plan_artwork(ai, {}, [], image, image, [image], "", root), corrected)
                reviewed_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "outside its canvas"):
                    plan_artwork(ai, {}, [], image, image, [image], "", root)
            self.assertEqual(json.loads(raw_path.read_text()), raw)

    def test_reference_thumbnails_are_downloaded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "references.json").write_text(json.dumps([{"id": "YvHA-iZxv4A", "notes": "Tight face crop"}]))
            encoded = io.BytesIO()
            Image.new("RGB", (1280, 720), "blue").save(encoded, "JPEG")
            with patch("milloclips.thumbnails.urlopen", return_value=io.BytesIO(encoded.getvalue())) as download:
                first = prepare_thumbnail_references(root)
                self.assertEqual(prepare_thumbnail_references(root), first)
                self.assertEqual(download.call_count, 1)
            self.assertTrue((root / "references" / "contact-sheet.jpg").exists())

    def test_feedback_cli_writes_only_the_requested_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = root / "preferences.md"
            for scope, note in (("channel", "Clear subjects"), ("video", "Replace this hook")):
                with patch("milloclips.cli.THUMBNAIL_PREFERENCES", channel), patch("sys.stdout", io.StringIO()), patch(
                    "sys.argv", ["milloclips", "thumbnails", str(root), "--feedback-scope", scope, "--feedback", note]
                ):
                    main()
            self.assertEqual(channel.read_text(), "\n- Clear subjects\n")
            self.assertEqual((root / "thumbnail-feedback.reviewed.md").read_text(), "\n- Replace this hook\n")

    def test_video_feedback_does_not_leak_into_other_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = root / "preferences.md"
            channel.write_text("Use clear game subjects", encoding="utf-8")
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "thumbnail-feedback.reviewed.md").write_text("Replace the Baron hook", encoding="utf-8")
            with patch("milloclips.ai.THUMBNAIL_PREFERENCES", channel):
                self.assertIn("Replace the Baron hook", thumbnail_preferences(first))
                self.assertNotIn("Baron", thumbnail_preferences(second))
                self.assertEqual(thumbnail_preferences(), "Use clear game subjects")

    def test_support_terminology_and_feedback_invalidate_concept_cache(self):
        self.assertEqual(thumbnail_copy("MI FIDDLE EN APOYO"), "MI FIDDLE SUPPORT")
        self.assertEqual(thumbnail_copy("apoyos y apoyo"), "apoyos y support")
        with tempfile.TemporaryDirectory() as tmp:
            ai = object.__new__(AI)
            ai.mock = False
            ai.vod_dir = Path(tmp)
            prompts = []
            def answer(prompt, *_args, **_kwargs):
                prompts.append(prompt)
                return {"concepts": []} if "Create exactly" in prompt else {"reviews": []}
            ai._json = answer
            with patch("milloclips.ai.thumbnail_preferences", return_value="Prefer concrete mistakes"):
                ai.thumbnail_concepts([], "", [])
                ai.thumbnail_concepts([], "", [])
            self.assertEqual(len(prompts), 2)
            self.assertTrue(all("Prefer concrete mistakes" in prompt for prompt in prompts))
            with patch("milloclips.ai.thumbnail_preferences", return_value="Prefer unusual builds"):
                ai.thumbnail_concepts([], "", [])
            self.assertEqual(len(prompts), 4)
            self.assertTrue(all("Prefer unusual builds" in prompt for prompt in prompts[2:]))

    def test_reaction_outlines_silhouette_without_outlining_clipped_photo_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "reaction.png"
            Image.new("RGBA", (100, 100), "red").save(photo)
            for side, rim_x, interior_x in (("right", 667, 675), ("left", 613, 605)):
                canvas = Image.new("RGB", (1280, 720), "black")
                _reaction(canvas, photo, side)
                self.assertEqual(canvas.getpixel((rim_x, 300)), (0, 0, 0))
                self.assertEqual(canvas.getpixel((interior_x, 300)), (255, 0, 0))
            source = Image.new("RGBA", (100, 100))
            ImageDraw.Draw(source).ellipse((20, 10, 80, 90), fill="red")
            source.save(photo)
            canvas = Image.new("RGB", (1280, 720), "black")
            _reaction(canvas, photo, crop=[0, 0, 1, 1])
            self.assertIn((255, 255, 255), {color for _, color in canvas.getcolors(canvas.width * canvas.height)})
            self.assertEqual(canvas.getpixel((667, 300)), (0, 0, 0))

    def test_contact_sheet_labels_use_complete_wrapped_video_titles(self):
        title = "CARREO CON UN PICK INESPERADO Y UNA BUILD QUE CAMBIA LA PARTIDA"
        rows = [{"number": 1, "title": title, "headline": "PICK INESPERADO"}]
        with patch("milloclips.thumbnails.ImageDraw.ImageDraw.multiline_text") as caption:
            sheet = _contact_sheet([Image.new("RGB", (480, 270))], rows, 480, 21)
        text = caption.call_args.args[1]
        self.assertEqual(" ".join(text.split()), "01 / " + title)
        self.assertGreater(len(text.splitlines()), 1)
        self.assertGreater(sheet.height, 350)

    def test_audited_natural_copy_is_accepted_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai = object.__new__(AI)
            ai.mock = False
            ai.vod_dir = Path(tmp)
            concept = {"title": "MI FIDDLE SUPPORT", "headline": "FIDDLE SUPPORT",
                       "hook": "unusual pick/role/challenge", "template": "identity",
                       "expression": "hold-on", "beat_id": 7, "evidence": "Fiddlesticks support",
                       "champion": "Fiddlesticks", "assets": [{"kind": "item", "key": "../bad"}]}
            replies = [{"concepts": [concept]}, {"reviews": [{"index": 0, "accepted": True}]}]
            ai._json = lambda *_args, **_kwargs: replies.pop(0)
            beats = [{"id": 7, "text": "Fiddlesticks support gana la pelea"}]
            concepts = ai.thumbnail_concepts(beats, "", [])
            self.assertEqual(concepts[0]["headline"], "FIDDLE SUPPORT")
            self.assertEqual(concepts[0]["assets"], [])
            self.assertEqual(ai.thumbnail_concepts(beats, "", []), concepts)
            self.assertEqual(replies, [])

    def test_concept_json_example_includes_required_reaction_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "smiling.png"
            Image.new("RGBA", (10, 10), "red").save(photo)
            ai = AI(Path(tmp), mock=True)
            ai.mock = False
            prompts = []
            concept = {"title": "CARREO CON FIDDLESTICKS SUPPORT", "headline": "FIDDLE SUPPORT",
                       "hook": "unusual pick/role/challenge", "template": "identity", "expression": "smiling",
                       "reaction_reason": "A confident smile fits the unusual pick succeeding.",
                       "beat_id": 7, "evidence": "Fiddlesticks support"}
            def answer(prompt, *_args, **_kwargs):
                prompts.append(prompt)
                return {"concepts": [concept]} if "Create exactly" in prompt else {"reviews": [{"index": 0, "accepted": True}]}
            ai._json = answer
            result = ai.thumbnail_concepts([{"id": 7, "text": "Fiddlesticks support gana la pelea"}], "", [],
                                           reactions=[{"expression": "smiling", "path": str(photo)}])
            example = next(line for line in prompts[0].splitlines() if line.startswith('{"concepts":'))
            self.assertIn("reaction_reason", json.loads(example)["concepts"][0])
            self.assertEqual(result[0]["reaction_reason"], concept["reaction_reason"])

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

    def test_concepts_accept_evidence_quotes_that_only_join_spoken_words(self):
        ai = object.__new__(AI)
        ai.mock = False
        ai.vod_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ai.vod_dir)
        ai._json = lambda *_args, **_kwargs: {"concepts": [{
            "title": "Unshot en Udyr", "headline": "UNSHOT", "hook": "high-stakes/amazing moment",
            "template": "action", "expression": "shocked", "beat_id": 7, "evidence": "one shot",
        }], "reviews": [{"index": 0, "accepted": True, "reason": "Evidenced one-shot elimination."}]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "Oh my god que me dio oneshot", "reason": "Eliminada de un golpe"}]
        concepts = ai.thumbnail_concepts(beats, "Eliminada de un golpe", [])
        self.assertEqual([concept["headline"] for concept in concepts], ["UNSHOT"])

    def test_concepts_accept_champion_gameplay_without_a_standout_play(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai = AI(Path(tmp))
            concept = {"title": "Hoy toca Fiddlesticks jungla", "headline": "TOCA FIDEL",
                       "hook": "champion/gameplay", "template": "identity", "expression": "smiling",
                       "beat_id": 7, "evidence": "Fiddlesticks jungla", "champion": "Fiddlesticks", "assets": []}
            beats = [{"id": 7, "type": "FARMING", "importance": 1,
                      "text": "Hoy toca Fiddlesticks jungla", "reason": "Ordinary gameplay"}]
            with patch.object(ai, "_json", side_effect=[{"concepts": [concept]}, {
                "reviews": [{"index": 0, "accepted": True, "reason": "Honest champion-led gameplay"}]
            }]):
                result = ai.thumbnail_concepts(beats, "Ordinary jungle game", [])
            self.assertEqual([item["hook"] for item in result], ["champion/gameplay"])
            self.assertEqual(result[0]["assets"], [])

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
            "template": "action", "expression": "overly-hyped", "beat_id": 7, "evidence": "podría ganar",
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
            "template": "action", "expression": "overly-hyped", "beat_id": 7, "evidence": "Tristana reset W",
        }]}
        beats = [{"id": 7, "type": "COMBAT", "importance": 5,
                  "text": "Tristana reset W", "reason": "Visible reset"}]
        self.assertEqual(ai.thumbnail_concepts(beats, "Tristana reset", []), [])

    def test_one_thumbnail_uses_final_edit_frames_without_comparison(self):
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
            for expression in ("shocked", "hold-on", "overly-hyped", "smiling"):
                Image.new("RGBA", (80, 80), "red").save(faces / f"{expression}.png")
            sheet = render_thumbnails(vod, faces, ai=AI(vod, mock=True))
            render_thumbnails(vod, faces, ai=AI(vod, mock=True))
            self.assertEqual(sheet.parent, vod / "renders" / "thumbnails")
            rows = json.loads((sheet.parent / "variants.json").read_text())
            self.assertEqual(len(rows), 1)
            self.assertTrue(all(row["beat_id"] in {1, 3} and Path(row["frame"]).exists() for row in rows))
            self.assertTrue((vod / "thumbnail-brief.json").exists())
            self.assertTrue((vod / "renders" / "thumbnail.png").exists())
            selection = json.loads((sheet.parent / "selection.json").read_text())
            self.assertEqual(selection["status"], "selected")
            self.assertEqual(selection["source"], "generated")
            self.assertFalse((sheet.parent / "selection.raw.json").exists())
            Image.new("RGBA", (80, 80), "blue").save(faces / "shocked.png")
            render_thumbnails(vod, faces, ai=AI(vod, mock=True))
            self.assertEqual(json.loads((sheet.parent / "selection.json").read_text())["status"], "selected")
            self.assertFalse((sheet.parent / "review.raw.json").exists())
            with Image.open(rows[0]["render"]) as rendered:
                self.assertEqual(rendered.size, (1280, 720))
            with Image.open(rows[0]["render"]) as gameplay:
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
