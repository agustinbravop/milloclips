from __future__ import annotations

import json
import re
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import ImageFont

from milloclips.ai import AI
from milloclips.editing import (
    _ass_text,
    _beat_intervals,
    _download_asset,
    _enforce_duration,
    _long_scene_ranges,
    _long_transitions,
    _avoid_split_word,
    _resolve_evidence,
    _retime_short,
    _retime_multi_short,
    _short_evidence,
    _meaningful_streamer_speech,
    _complete_speech_exit,
    _extend_short_payoff,
    _validate_short_candidates,
    _subtitle_blocks,
    _validate_caption_blocks,
    _write_short_subtitles,
    _short_caption_words,
    analyze,
    find_shorts,
    plan_long,
    render_long,
    select_game,
    short_candidates,
    render_short,
    transcript_words,
)
from milloclips.media import render_long_video, render_vertical, title_music_window
from milloclips.reports import shorts_report
from milloclips.scan import detect_games


class GameDetectionTests(unittest.TestCase):
    def test_ignores_game_too_short_for_output(self) -> None:
        points = [
            {"timestamp": 0, "state": "IN_GAME"},
            {"timestamp": 300, "state": "POST_GAME"},
        ]
        self.assertEqual(detect_games(points, 360), [])

    def test_detects_game_and_attaches_surrounding_states(self) -> None:
        points = [
            {"timestamp": 0, "state": "OTHER"},
            {"timestamp": 100, "state": "CHAMP_SELECT"},
            {"timestamp": 200, "state": "LOADING"},
            {"timestamp": 230, "state": "IN_GAME"},
            {"timestamp": 1430, "state": "POST_GAME"},
            {"timestamp": 1490, "state": "OTHER"},
        ]
        games = detect_games(points, 1600)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["champ_select_start"], 100)
        self.assertEqual(games[0]["loading_start"], 200)
        self.assertEqual(games[0]["gameplay_start"], 230)
        self.assertEqual(games[0]["gameplay_end"], 1430)
        self.assertEqual(games[0]["postgame_end"], 1490)

    def test_merges_short_gameplay_interruption(self) -> None:
        points = [
            {"timestamp": 0, "state": "IN_GAME"},
            {"timestamp": 300, "state": "OTHER"},
            {"timestamp": 350, "state": "IN_GAME"},
            {"timestamp": 900, "state": "POST_GAME"},
        ]
        games = detect_games(points, 960)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["duration"], 900)


class GameRankingTests(unittest.TestCase):
    def test_mock_ranks_games_by_beat_importance(self) -> None:
        ai = AI(Path("."), mock=True)
        ranked = ai.rank_games([
            {"game_id": "game-001", "duration": 900, "beats": [{"importance": 2}, {"importance": 1}]},
            {"game_id": "game-002", "duration": 1000, "beats": [{"importance": 5}, {"importance": 4}]},
        ])
        self.assertEqual([item["game_id"] for item in ranked], ["game-002", "game-001"])
        self.assertEqual(ranked[0]["score"], 9)


class EditingTests(unittest.TestCase):
    def test_short_payoff_keeps_three_seconds_and_finishes_delayed_reaction(self) -> None:
        original = {"source_start": 100, "source_end": 110, "relative_start": 0, "relative_end": 10, "duration": 10}
        words = [{"word": "what", "start": 12.5, "end": 13.2}, {"word": "happened", "start": 13.2, "end": 13.8},
                 {"word": "next", "start": 15, "end": 15.5}]
        extended = _extend_short_payoff(original, words, 100, 20)
        self.assertAlmostEqual(extended["relative_end"], 13.9)
        self.assertAlmostEqual(extended["source_end"], 113.9)
        self.assertEqual(original["relative_end"], 10)
        self.assertEqual(_extend_short_payoff(original, [], 100, 20)["relative_end"], 13)
        self.assertEqual(_extend_short_payoff(original, [], 100, 12)["relative_end"], 12)
        already_complete = {**original, "source_end": 115, "relative_end": 15, "duration": 15, "payoff_end": 10}
        self.assertEqual(_extend_short_payoff(already_complete, [], 100, 30)["relative_end"], 15)

    def test_rendered_caption_punctuation_removes_exclamations_and_periods_only(self) -> None:
        font = ImageFont.truetype(str(Path(__file__).parents[1] / "milloclips/fonts/Montserrat-Black.ttf"), 60)
        rendered = _ass_text([{"text": text} for text in ("¡qué!", "pasó.", "ahora?", "¿otra?")], 0, font)
        displayed = [text.rsplit("}", 1)[-1] for text in rendered]
        self.assertEqual(displayed, ["QUÉ", "PASÓ", "AHORA?", "¿OTRA?"])

    def test_whole_vod_excludes_slots_and_silent_league_before_judging(self) -> None:
        class FakeAI:
            mock = False
            def shorts(self, beats):
                return [{"title": title, "payoff_beat_id": index,
                         "scenes": [{"start_beat_id": index, "end_beat_id": index}]}
                        for index, title in ((0, "slots"), (2, "silent league"), (4, "spoken league"))]
            def verify_short(self, proposal, beats, frames):
                start, end = proposal["scenes"][0]["start"], proposal["scenes"][0]["end"]
                return {"title": proposal["title"], "story": "Complete story", "hook": "Setup", "payoff": "Reaction",
                        "segments": [{"start": start, "end": end}],
                        "hook_evidence": {"kind": "visual", "before_time": start, "time": start + 1, "detail": "visible opening event"},
                        "payoff_evidence": {"kind": "visual", "before_time": end - 3, "time": end - 2, "detail": "visible complete outcome"},
                        **({"streamer_evidence": {"kind": "speech", "time": 41, "quote": "esto no puede salir bien"}}
                           if proposal["title"] == "spoken league" else {})}
            def choose_shorts(self, candidates, images):
                self.seen = [item["title"] for item in candidates]
                return [0]
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.sample_frames", return_value=[
            {"path": f"frame_{index}.jpg"} for index in range(20)
        ]):
            directory = Path(temporary)
            game = {"id": "vod", "gameplay_start": 0, "gameplay_end": 60, "league_ranges": [{"start": 20, "end": 60}]}
            identity = {"path": str(Path("source.mp4").resolve()), "size": None, "mtime_ns": None}
            metadata = {"game_id": "vod", "gameplay_start": 0, "editorial_end": 60, "source_identity": identity,
                        "transcript_edits": {name: None for name in ("transcript.reviewed.json", "short-transcript.json", "short-transcript.reviewed.json")}}
            beats = [{"id": i, "start": i * 10, "end": (i + 1) * 10, "phase": "IN_GAME", "type": "REACTION",
                      "importance": 4, "text": "A different useful spoken phrase"} for i in range(6)]
            for name, data in {"games.json": {"games": [game]}, "selection.json": {"game_id": "vod"},
                               "manifest.json": {"source": "source.mp4", "duration": 60}, "beats.json": {**metadata, "beats": beats},
                               "transcript.json": {"words": [{"word": word, "start": 41 + i * 0.5, "end": 41.4 + i * 0.5}
                                                             for i, word in enumerate("esto no puede salir bien".split())]}}.items():
                (directory / name).write_text(json.dumps(data))
            ai = FakeAI()
            result = find_shorts(directory, ai)
            self.assertEqual(ai.seen, ["spoken league"])
            self.assertEqual(result[0]["streamer_evidence"]["quote"], "esto no puede salir bien")

    def test_speech_exit_finishes_setup_longer_than_three_seconds(self) -> None:
        words = [{"word": "word", "start": index, "end": index + 0.8} for index in range(8)]
        words.append({"word": "next", "start": 9, "end": 9.5})
        self.assertAlmostEqual(_complete_speech_exit(1, words, (0, 10)), 7.9)

    def test_meaningful_speech_requires_complete_noncorrupt_phrase(self) -> None:
        words = [{"word": word, "start": index, "end": index + 0.8}
                 for index, word in enumerate("esto no puede salir bien".split())]
        evidence = {"kind": "speech", "time": 0, "quote": "esto no puede salir bien"}
        self.assertIsNotNone(_meaningful_streamer_speech(evidence, words, [(0, 6)], []))
        self.assertIsNotNone(_meaningful_streamer_speech([{"kind": "visual"}, evidence], words, [(0, 6)], []))
        self.assertIsNone(_meaningful_streamer_speech({"kind": "speech", "time": 0, "quote": "El equipo Krag ha ganado la batalla"}, words, [(0, 6)], []))
        self.assertIsNone(_meaningful_streamer_speech(evidence, words, [(0, 3)], []))
        self.assertIsNone(_meaningful_streamer_speech(evidence, words, [(0, 6)], [(0, 6)]))
        self.assertIsNone(_meaningful_streamer_speech({"kind": "speech", "time": 0, "quote": "Oh my god"}, words, [(0, 6)], []))
        self.assertIsNone(_meaningful_streamer_speech(None, words, [(0, 6)], []))

    def test_short_duration_allows_complete_sixty_second_story(self) -> None:
        candidate = {"source_start": 0, "source_end": 60, "relative_start": 0, "relative_end": 60, "duration": 60}
        self.assertEqual(_validate_short_candidates([candidate], 0, 100, 100), [candidate])
        with self.assertRaisesRegex(ValueError, "60 seconds"):
            _validate_short_candidates([{**candidate, "source_end": 61, "relative_end": 61, "duration": 61}], 0, 100, 100)

    def test_word_safe_exit_handles_overlapping_transcript_words(self) -> None:
        words = [{"start": 1, "end": 2}, {"start": 1.9, "end": 2.4}, {"start": 2.4, "end": 3}]
        self.assertEqual(_avoid_split_word(0, 1.5, words, (0, 4)), (0, 2.4))
        self.assertEqual(_avoid_split_word(2.1, 3.5, words, (0, 4)), (1, 3.5))

    def test_short_verifier_uses_labeled_source_not_beat_claims(self) -> None:
        ai = AI(Path("."), mock=True)
        ai.mock = False
        ai._json = Mock(return_value={"reject": True, "reason": "Only farming is visible"})
        result = ai.verify_short(
            {"title": "Crowstorm teamfight", "scenes": [{"start": 10, "end": 30}]},
            [{"start": 10, "end": 30, "text": "vamos", "summary": "invented multikill"}],
            [{"timestamp": 12, "path": "before.jpg"}, {"timestamp": 22, "path": "after.jpg"}],
        )
        self.assertTrue(result["reject"])
        prompt = ai._json.call_args.args[0]
        self.assertNotIn("invented multikill", prompt)
        self.assertIn("neutral jungle monsters/minions", prompt)
        self.assertEqual(ai._json.call_args.kwargs["image_labels"],
                         ["Source image at 12 seconds", "Source image at 22 seconds"])

    def test_short_callback_does_not_replace_the_long_opening_premise(self) -> None:
        beats = [
            {"id": 8, "start": 0, "end": 20, "phase": "IN_GAME", "importance": 5, "keep": True},
            {"id": 9, "start": 20, "end": 40, "phase": "IN_GAME", "importance": 5, "keep": True},
        ]

        class FakeAI:
            mock = False

            def plan_long(self, _beats: list[dict]) -> dict:
                return {"scenes": [{"start_beat_id": 8, "end_beat_id": 9, "purpose": "ElmilloR play"}],
                        "selected_ids": [8, 9], "cold_open_beat_id": 8, "story": "test"}

        def refine(_directory, _ai, _game, _beats, _scenes, _cache, cold_open_beat_id, _force):
            self.assertIsNone(cold_open_beat_id)
            return ([{"source_start": 100, "source_end": 650}],
                    {"source_start": 110, "source_end": 118, "relative_start": 10, "relative_end": 18})

        callback = {"title": "Teammate ACE", "segments": [{"relative_start": 20, "relative_end": 40}]}
        game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 700}
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.selected_game", return_value=game), \
                patch("milloclips.editing._selected_beats", return_value=beats), \
                patch("milloclips.editing._long_scene_ranges", return_value=[{"first_index": 0, "last_index": 1,
                      "start_beat_id": 8, "end_beat_id": 9, "purpose": "ElmilloR play"}]), \
                patch("milloclips.editing._refine_long_scenes", side_effect=refine), \
                patch("milloclips.editing._apply_long_intro", side_effect=lambda _root, _ai, plan, *_args: plan), \
                patch("milloclips.editing.short_candidates", return_value=[callback]):
            directory = Path(temporary)
            (directory / "short-candidates.json").write_text("{}")
            (directory / "manifest.json").write_text(json.dumps({"source": "source.mp4", "duration": 700}))
            plan = plan_long(directory, FakeAI(), force=True)
            self.assertEqual(plan["intro"]["kind"], "setup")
            self.assertEqual(plan["intro"]["source_start"], 100)

    def test_long_prompt_requires_elmiillor_as_cold_open_subject(self) -> None:
        ai = AI(Path("."), mock=True)
        ai.mock = False
        beat = {"id": 1, "start": 0, "end": 20, "phase": "IN_GAME", "type": "COMBAT",
                "importance": 5, "keep": True, "text": "play", "reason": "play"}
        with patch.object(ai, "_json", return_value={"scenes": [], "cold_open_beat_id": 1}) as request:
            ai.plan_long([beat])
        self.assertIn("ElmilloR himself", request.call_args.args[0])

    def test_long_cut_moves_mid_word_start_to_previous_pause(self) -> None:
        words = [
            {"word": "es", "start": 9.0, "end": 9.3},
            {"word": "no", "start": 10.0, "end": 10.2},
            {"word": "siquiera", "start": 10.2, "end": 10.8},
        ]
        self.assertEqual(_avoid_split_word(10.5, 12, words, (8, 15)), (9.9, 12))

    def test_long_transition_timeline_includes_cards_and_every_scene(self) -> None:
        plan = {"cold_open": {"source_start": 10, "source_end": 16}, "title_duration": 2.5,
                "segments": [{"source_start": 20, "source_end": 30}, {"source_start": 40, "source_end": 55}]}
        transitions = _long_transitions(plan)
        self.assertEqual([item["output_time"] for item in transitions], [5.5, 7.5, 17.5, 32.0])
        self.assertEqual(transitions[-1]["label"], "result -> end card")

    def test_shared_artwork_download_uses_browser_user_agent(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"image"

        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.urlopen", return_value=Response()) as open_url:
            _download_asset(Path(temporary) / "icon.png", "https://elmiillor.com/favicon.png")
        request = open_url.call_args.args[0]
        self.assertIn("Mozilla", request.get_header("User-agent"))

    def test_long_plan_uses_complete_scenes_and_keeps_actual_ending(self) -> None:
        beats = [
            {"id": index, "start": index * 10, "end": (index + 1) * 10, "phase": "IN_GAME",
             "importance": 4, "keep": True}
            for index in range(60)
        ]
        scenes = _long_scene_ranges(beats, {"scenes": [
            {"start_beat_id": 0, "end_beat_id": 25, "purpose": "premise"},
            {"start_beat_id": 30, "end_beat_id": 56, "purpose": "escalation"},
        ]})
        self.assertEqual(scenes[0]["start_beat_id"], 0)
        self.assertEqual(scenes[-1]["end_beat_id"], 59)
        self.assertIn("actual final fight", scenes[-1]["purpose"])

        ending = {"start_beat_id": 50, "end_beat_id": 56, "purpose": "resolved defeat and reaction", "required": True}
        scenes = _long_scene_ranges(beats, {"scenes": [ending]})
        self.assertEqual(scenes[-1]["end_beat_id"], 56)
        self.assertTrue(scenes[-1]["final_scene"])

    def test_render_long_skips_ffmpeg_when_the_sidecar_still_proves_the_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            (root / "renders").mkdir()
            (root / "renders" / "long-rough.mp4").write_bytes(b"render")
            plan = {"long_version": 16, "intro_policy_version": 1, "end_card_duration": 12.0,
                    "cold_open": {"source_start": 0, "source_end": 7},
                    "segments": [{"source_start": 20, "source_end": 600}]}
            (root / "long-plan.json").write_text(json.dumps(plan))
            (root / "manifest.json").write_text(json.dumps({"source": str(source), "duration": 900}))
            cards = (root / "title.mp4", root / "end.mp4", root / "music.m4a")
            for card in cards:
                card.write_bytes(b"card")
            def measured(*_args: object) -> dict:
                return {"duration": json.loads((root / "long-plan.json").read_text())["duration"],
                        "width": 1280, "height": 720, "audio": True}

            with patch("milloclips.editing._long_cards", return_value=cards), \
                 patch("milloclips.editing.render_long_video", side_effect=lambda *args, **kwargs: args[5].write_bytes(b"render")) as encode, \
                 patch("milloclips.editing.media_info", side_effect=measured), \
                 patch("milloclips.editing.render_long_review"):
                render_long(root)
                self.assertEqual(encode.call_count, 1)
                sidecar = json.loads((root / "renders" / "long-rough.json").read_text())
                self.assertEqual(sidecar["width"], 1280)

                render_long(root)
                self.assertEqual(encode.call_count, 1)

                # A changed plan invalidates the render.
                plan["segments"] = [{"source_start": 20, "source_end": 400}]
                (root / "long-plan.json").write_text(json.dumps(plan))
                render_long(root)
                self.assertEqual(encode.call_count, 2)

    def test_render_short_skips_ffmpeg_when_the_render_is_already_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            source.write_bytes(b"source")
            subtitles = directory / "short-01.ass"
            subtitles.write_text("captions")
            candidate = {"title": "Test", "render_name": "short-01", "output_number": 1,
                         "source_start": 10, "source_end": 20, "relative_start": 0, "relative_end": 10,
                         "segments": [{"source_start": 10, "source_end": 20, "relative_start": 0, "relative_end": 10}]}
            (directory / "manifest.json").write_text(json.dumps({"source": str(source), "duration": 100}))
            (directory / "games.json").write_text(json.dumps({"games": [{"id": "game-001", "gameplay_start": 10, "gameplay_end": 100}]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            stat = source.stat()
            (directory / "short-candidates.json").write_text(json.dumps({
                "version": 8, "output_version": 21, "evidence_version": 7, "cuts_version": 10, "callback_version": 2,
                "exclusions": [], "game_id": "game-001", "gameplay_start": 10, "editorial_end": 100,
                "transcript_edits": {"transcript.reviewed.json": None, "short-transcript.json": None,
                                    "short-transcript.reviewed.json": None},
                "source_identity": {"path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                "candidates": [candidate]}))
            (directory / "layout.json").write_text(json.dumps({"facecam": [0, 0, 0.25, 0.25]}))

            def fake_render(*args: object) -> None:
                output = args[5]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"video")

            with patch("milloclips.editing._write_short_subtitles", return_value=subtitles), \
                 patch("milloclips.editing.render_vertical", side_effect=fake_render) as encode, \
                 patch("milloclips.editing.media_info", return_value={"duration": 10, "width": 1080, "height": 1920, "audio": True}):
                render_short(directory)
                self.assertEqual(encode.call_count, 1)
                render_short(directory)
                self.assertEqual(encode.call_count, 1)

                # A changed caption invalidates the render.
                subtitles.write_text("different captions")
                render_short(directory)
                self.assertEqual(encode.call_count, 2)

    def test_long_render_orders_cold_open_title_gameplay_and_end_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.media.run_ffmpeg") as ffmpeg:
            root = Path(temporary)
            render_long_video(Path("source.mp4"), [{"source_start": 20, "source_end": 40}],
                              {"source_start": 30, "source_end": 37}, root / "title.mp4", root / "end.mp4",
                              root / "long.mp4")
        command = ffmpeg.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[openingv][openinga]concat=n=1:v=1:a=1[bodyv][bodya]", filters)
        self.assertIn("[bodyclock][endv]xfade=transition=fade:duration=0.5:offset=28.0", filters)
        self.assertIn("xfade=transition=fade:duration=0.5:offset=6.5", filters)
        self.assertIn("[introv][v1]xfade=transition=fade:duration=0.5:offset=8.5", filters)
        self.assertIn("[2:a]atrim=duration=1.5", filters)
        self.assertIn("[3:a]atrim=duration=7.5", filters)
        self.assertIn("[1:v]trim=start=5:end=25,setpts", filters)
        self.assertIn("[1:a]atrim=start=5:end=25,asetpts", filters)
        self.assertNotIn("zoompan=", filters)
        self.assertNotIn("-loop", command)
        self.assertEqual(command.count("-ss"), 2)
        self.assertEqual(command.count("source.mp4"), 2)

    def test_title_music_straddles_the_card_in_both_directions(self) -> None:
        self.assertEqual(title_music_window(6.0, 5.0),
                          {"delay": 4.5, "length": 7.0, "fade_in": 2.0, "fade_out": 1.0})
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.media.run_ffmpeg") as ffmpeg:
            root = Path(temporary)
            render_long_video(Path("source.mp4"), [{"source_start": 20, "source_end": 40}],
                              {"source_start": 30, "source_end": 36}, root / "title.mp4", root / "end.mp4",
                              root / "long.mp4", title_duration=5.0, end_duration=12.0,
                              title_music=root / "sting.webm", title_music_start=42.5)
            music = str(root / "sting.webm")
        command = ffmpeg.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        # Card starts with the dissolve at 5.5s; music starts at 4.5s and peaks at 6.5s.
        self.assertEqual(command[command.index(music) - 5:command.index(music)], ["-ss", "42.5", "-t", "7.0", "-i"])
        self.assertIn("afade=t=in:st=0:d=2.0:curve=qua,afade=t=out:st=6.0:d=1.0:curve=qua,adelay=4500:all=1[musica]", filters)
        self.assertIn("volume=0.07734375", filters)
        self.assertIn("[rawv]fps=30,geq=lum='16+(lum(X,Y)-16)*(1-pow(1-T,3))'", filters)
        self.assertIn("enable='lt(t,1)'[v]", filters)
        self.assertIn("amix=inputs=2:duration=first:dropout_transition=0:normalize=0", filters)
        # loudnorm has to run before the mix or its dynamic gain reshapes both fades.
        self.assertLess(filters.index("loudnorm"), filters.index("amix"))

    def test_end_music_starts_before_the_muted_end_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.media.run_ffmpeg") as ffmpeg:
            root = Path(temporary)
            render_long_video(Path("source.mp4"), [{"source_start": 20, "source_end": 40}],
                              {"source_start": 30, "source_end": 36}, root / "title.mp4", root / "end.mp4",
                              root / "long.mp4", title_duration=5.0, end_duration=12.0,
                              end_music=root / "music.webm", end_music_start=42)
            music = str(root / "music.webm")
        command = ffmpeg.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertEqual(command[command.index(music) - 5:command.index(music)], ["-ss", "42", "-t", "13.0", "-i"])
        self.assertIn("[3:a]atrim=duration=11.5", filters)
        self.assertIn("volume=0[enda]", filters)
        self.assertIn("volume=0.103125", filters)
        # One-second lead plus two-second fade peaks one second after card appearance.
        self.assertIn("afade=t=in:st=0:d=2.0:curve=tri,afade=t=out:st=12.0:d=1:curve=qua,adelay=28500:all=1[endmusic]", filters)

    def test_legacy_matching_transcript_is_reused(self) -> None:
        class FakeAI:
            def transcribe(self, *_args: object) -> dict:
                raise AssertionError("matching cached transcript must be reused")

            def classify_beats(self, beats: list[dict], _frames: list[Path]) -> list[dict]:
                return [{**beat, "type": "COMBAT", "importance": 4, "keep": True, "reason": "test"} for beat in beats]

        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.sample_frames", return_value=[]), patch(
            "milloclips.editing.extract_audio"
        ) as extract:
            directory = Path(temporary)
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 120}
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            (directory / "manifest.json").write_text(json.dumps({"source": "source.mp4"}))
            (directory / "transcript.json").write_text(json.dumps({"words": [{"word": "vamos", "start": 0, "end": 1}]}))
            (directory / "beats.json").write_text(
                json.dumps({"game_id": "game-001", "target_beat_seconds": 15, "beats": [{"id": 0, "start": 0, "end": 20, "phase": "IN_GAME"}]})
            )

            analyze(directory, FakeAI(), force=True)

            extract.assert_not_called()
            transcript = json.loads((directory / "transcript.json").read_text())
            self.assertEqual(transcript["game_id"], "game-001")
            self.assertEqual(transcript["audio_start"], 100)
            self.assertEqual(transcript["audio_end"], 120)

    def test_short_selection_verifies_outcomes_and_preserves_story_segments(self) -> None:
        class FakeAI:
            seen: list[dict] = []
            mock = True
            short_calls = 0

            def shorts(self, beats: list[dict]) -> list[dict]:
                self.short_calls += 1
                self.seen = beats
                return [
                    {"title": "Full game arc", "scenes": [
                        {"start_beat_id": 0, "end_beat_id": 1},
                        {"start_beat_id": 4, "end_beat_id": 5},
                        {"start_beat_id": 8, "end_beat_id": 9},
                    ], "payoff_beat_id": 9},
                    {"title": "Only a plan", "payoff_beat_id": 11, "scenes": [{"start_beat_id": 10, "end_beat_id": 11}]},
                ]

            def verify_short(self, proposal: dict, _beats: list[dict], _frames: list[dict]) -> dict:
                if proposal["title"] == "Only a plan":
                    return {"reject": True}
                return {"title": "Actual payoff", "hook": "promise", "payoff": "reversal", "story": "promise then reversal",
                        "hook_evidence": {"kind": "visual", "before_time": 1, "time": 2, "detail": "clear setup in gameplay"},
                        "payoff_evidence": {"kind": "visual", "before_time": 96, "time": 97, "detail": "surprising end in gameplay"},
                        "segments": [{"start": 2, "end": 18}, {"start": 42, "end": 58}, {"start": 82, "end": 98}]}

            def choose_shorts(self, candidates: list[dict], _images: list[Path]) -> list[int]:
                self.seen_candidates = candidates
                return [0]

        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.sample_frames", return_value=[
            {"path": f"frame_{index}.jpg"} for index in range(20)
        ]):
            directory = Path(temporary)
            game = {"id": "game-002", "gameplay_start": 100, "gameplay_end": 240}
            source_identity = {"path": str(Path("source.mp4").resolve()), "size": None, "mtime_ns": None}
            transcript_edits = {name: None for name in ("transcript.reviewed.json", "short-transcript.json", "short-transcript.reviewed.json")}
            cache_key = {"game_id": "game-002", "gameplay_start": 100, "editorial_end": 240,
                         "transcript_edits": transcript_edits, "source_identity": source_identity}
            beats = [
                {
                    "id": index,
                    "start": index * 10,
                    "end": index * 10 + 10,
                    "phase": "IN_GAME",
                    "type": "FARMING" if index == 0 else "COMBAT",
                    "importance": 1 if index == 0 else 4,
                    "keep": index != 0,
                    "text": "context",
                    "reason": "test",
                }
                for index in range(12)
            ]
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-002"}))
            (directory / "beats.json").write_text(json.dumps({**cache_key, "beats": beats}))
            (directory / "manifest.json").write_text(json.dumps({"source": "source.mp4"}))
            (directory / "transcript.json").write_text(json.dumps({"words": []}))
            (directory / "short-candidates.json").write_text(json.dumps({"game_id": "game-001", "candidates": []}))
            ai = FakeAI()

            candidates = find_shorts(directory, ai)

            self.assertEqual(len(ai.seen), len(beats))
            self.assertEqual(ai.seen[0]["importance"], 1)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["title"], "Actual payoff")
            self.assertEqual(candidates[0]["duration"], 51)
            self.assertEqual([segment["source_start"] for segment in candidates[0]["segments"]], [101, 142, 182])
            self.assertEqual(json.loads((directory / "short-proposals.json").read_text())["version"], 8)
            self.assertEqual(len(ai.seen_candidates), 1)
            self.assertEqual(find_shorts(directory, ai), candidates)
            self.assertEqual(ai.short_calls, 1)
            beats[0]["reason"] = "changed analysis"
            (directory / "beats.json").write_text(json.dumps({**cache_key, "beats": beats}))
            self.assertEqual(find_shorts(directory, ai), candidates)
            self.assertEqual(ai.short_calls, 2)
            ai.choose_shorts = lambda _candidates, _images: []
            self.assertEqual(find_shorts(directory, ai, force=True)[0]["title"], "Actual payoff")
            (directory / "short-exclusions.reviewed.json").write_text(json.dumps({"game_id": "game-002", "ranges": [{"start": 0, "end": 20}]}))
            self.assertEqual(find_shorts(directory, ai), [])
            self.assertEqual(len(ai.seen), len(beats) - 2)

    def test_short_judge_uses_verified_complete_cuts_without_another_paid_edit(self) -> None:
        class FakeAI:
            events = []
            short_calls = 0
            verify_calls = 0

            def shorts(self, _beats):
                self.short_calls += 1
                return [{"title": str(index), "payoff_beat_id": index,
                         "scenes": [{"start_beat_id": index, "end_beat_id": index}]} for index in (0, 2, 4)] + [
                    {"title": "2 alias", "payoff_beat_id": 2, "scenes": [{"start_beat_id": 2, "end_beat_id": 2}]}]

            def verify_short(self, proposal, _beats, _frames):
                self.verify_calls += 1
                start, end = proposal["scenes"][0]["start"], proposal["scenes"][0]["end"]
                return {"title": proposal["title"], "story": "Supported event", "hook": "Premise", "payoff": "Outcome",
                        "hook_evidence": {"kind": "visual", "before_time": start, "time": start + 1, "detail": "visible opening event"},
                        "payoff_evidence": {"kind": "visual", "before_time": end - 3, "time": end - 2, "detail": "visible related outcome"},
                        "segments": ([{"start": start, "end": (start + end) / 2}, {"start": (start + end) / 2, "end": end}]
                                     if proposal["title"] == "4" else [{"start": start, "end": end}])}

            def choose_shorts(self, candidates, _images):
                self.events.append("judge")
                self.asserted_titles = [item["title"] for item in candidates]
                return [1, 1, True, 99, 0, 2]

            def refine_short(self, candidate, _words, _frames):
                self.events.append(candidate["title"])
                if candidate["title"] == "2" and not candidate.get("editing_feedback"):
                    return {"reject": True, "reason": "Missing ending context"}
                return {**candidate, "story": candidate["reason"]}

            def audit_short(self, *_args):
                raise AssertionError("Finished-cut audit must not run")

        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.sample_frames", side_effect=lambda *_args, **kw: [
            {"path": f"frame_{index:06d}.jpg"} for index in range(int(kw["end"] - kw["start"]))
        ]):
            directory = Path(temporary)
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 160}
            identity = {"path": str(Path("source.mp4").resolve()), "size": None, "mtime_ns": None}
            beats = [{"id": index, "start": index * 10, "end": (index + 1) * 10, "phase": "IN_GAME",
                      "text": "Complete event", "importance": 4, "type": "COMBAT"} for index in range(6)]
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": game["id"]}))
            (directory / "manifest.json").write_text(json.dumps({"source": "source.mp4"}))
            (directory / "transcript.json").write_text(json.dumps({"words": []}))
            (directory / "beats.json").write_text(json.dumps({"game_id": game["id"], "gameplay_start": 100,
                "editorial_end": 160, "source_identity": identity, "beats": beats,
                "transcript_edits": {name: None for name in ("transcript.reviewed.json", "short-transcript.json", "short-transcript.reviewed.json")}}))
            ai = FakeAI()
            result = find_shorts(directory, ai)
            self.assertEqual(ai.asserted_titles, ["0", "2", "4"])
            self.assertEqual(ai.events, ["judge"])
            self.assertEqual([item["title"] for item in result], ["2", "0"])
            self.assertEqual([item["render_name"] for item in result], ["short-01", "short-02"])
            self.assertEqual(find_shorts(directory, ai), result)
            self.assertEqual(ai.events, ["judge"])
            for name in ("short-verdicts.json", "short-candidates.json"):
                path = directory / name
                cached = json.loads(path.read_text())
                cached["evidence_version"] = 3
                path.write_text(json.dumps(cached))
            self.assertEqual(find_shorts(directory, ai), result)
            self.assertEqual(ai.short_calls, 1)
            self.assertEqual(ai.verify_calls, 8)
            with patch.object(ai, "refine_short", side_effect=AssertionError("No redundant edit")):
                self.assertEqual(find_shorts(directory, ai, force=True), result)

    def test_text_proposed_callback_keeps_visual_payoff_despite_corrupt_late_transcript(self) -> None:
        class FakeAI:
            mock = True

            def shorts(self, _beats: list[dict]) -> list[dict]:
                return self.callback_shorts(_beats, [])

            def visual_shorts(self, _frames: list[dict]) -> list[dict]:
                return []

            def callback_shorts(self, _beats: list[dict], _frames: list[dict]) -> list[dict]:
                return [{"title": "The teammate proves him wrong", "payoff_beat_id": 15,
                         "proposed_payoff": "The teammate survives while the streamer is dead",
                         "scenes": [{"start_beat_id": 0, "end_beat_id": 0},
                                    {"start_beat_id": 14, "end_beat_id": 14},
                                     {"start_beat_id": 14, "end_beat_id": 15}]}]

            def verify_short(self, proposal: dict, _beats: list[dict], _frames: list[dict]) -> dict:
                self.scenes = proposal["scenes"]
                return {"title": "The teammate proves him wrong", "story": "Dismissal, death, comeback",
                        "hook": "Tristana mala", "payoff": "Teammate survives",
                        "hook_evidence": {"kind": "speech", "time": 0.2, "quote": "Tristana mala"},
                        "payoff_evidence": {"kind": "visual", "before_time": 150, "time": 155,
                                            "detail": "the teammate is still alive after the fight"},
                        "segments": [{"start": scene["start"], "end": scene["end"]} for scene in proposal["scenes"]]}

            def choose_shorts(self, _candidates: list[dict], _images: list[Path]) -> list[int]:
                return [0]

        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.editing.sample_frames", return_value=[
            {"path": f"frame_{index}.jpg"} for index in range(20)
        ]):
            directory = Path(temporary)
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 280}
            beats = [{"id": index, "start": index * 10, "end": (index + 1) * 10,
                      "phase": "IN_GAME", "text": "Tristana mala" if index == 0 else "A A A A" if index >= 10 else "ordinary",
                      "importance": 4, "type": "COMBAT"} for index in range(18)]
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            (directory / "manifest.json").write_text(json.dumps({"source": "source.mp4"}))
            source_identity = {"path": str(Path("source.mp4").resolve()), "size": None, "mtime_ns": None}
            (directory / "beats.json").write_text(json.dumps({"game_id": "game-001", "gameplay_start": 100,
                                                                 "editorial_end": 280, "transcript_edits": {
                                                                     "transcript.reviewed.json": None, "short-transcript.json": None,
                                                                     "short-transcript.reviewed.json": None},
                                                                 "source_identity": source_identity, "beats": beats}))
            (directory / "transcript.json").write_text(json.dumps({"words": [
                {"word": "Tristana", "start": 0.2, "end": 0.5}, {"word": "mala", "start": 0.6, "end": 1.0}]}))
            frames_dir = directory / "frames" / "selected-game"
            frames_dir.mkdir(parents=True)
            for index in range(36):
                (frames_dir / f"frame_{index:06d}.jpg").write_bytes(b"")
            ai = FakeAI()
            candidates = find_shorts(directory, ai)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(len(candidates[0]["segments"]), 2)
            self.assertEqual(ai.scenes[-1]["start"], 140)
            self.assertFalse((directory / "short-callback-proposals.json").exists())
            original_verifier = ai.verify_short
            def corrupt_spoken_payoff(proposal, beats, frames):
                verdict = original_verifier(proposal, beats, frames)
                return {**verdict, "payoff_evidence": {"kind": "speech", "time": 155, "quote": "A A"}}
            (directory / "transcript.json").write_text(json.dumps({"words": [
                {"word": "Tristana", "start": 0.2, "end": 0.5}, {"word": "mala", "start": 0.6, "end": 1.0},
                {"word": "A", "start": 155, "end": 155.2}, {"word": "A", "start": 155.3, "end": 155.5}]}))
            with patch.object(ai, "verify_short", side_effect=corrupt_spoken_payoff):
                self.assertEqual(find_shorts(directory, ai, force=True), [])
            diagnostics = json.loads((directory / "short-diagnostics.json").read_text())
            self.assertTrue(any("unhealthy repetitive transcript" in item["reason"] for item in diagnostics["rejected"]))

    def test_short_gate_checks_spoken_hook_and_payoff_timing(self) -> None:
        words = [
            {"word": "imposible", "start": 10, "end": 10.5},
            {"word": "Graves", "start": 10.5, "end": 11},
            {"word": "sí", "start": 20, "end": 20.3},
            {"word": "estaba", "start": 20.3, "end": 20.7},
        ]
        segment = {"relative_start": 10, "relative_end": 22}
        self.assertTrue(_short_evidence({"kind": "speech", "time": 10, "quote": "Imposible Graves"}, segment, words, [], hook=True))
        self.assertTrue(_short_evidence({"kind": "speech", "time": 20, "quote": "sí estaba"}, segment, words, [], hook=False))
        self.assertTrue(_short_evidence({"kind": "speech", "time": 10, "quote": "imposible Graves"},
                                        {"relative_start": 0, "relative_end": 40}, words, [], hook=True))
        self.assertTrue(_short_evidence({"kind": "speech", "time": 20, "quote": "sí estaba"},
                                        {"relative_start": 0, "relative_end": 40}, words, [], hook=False))
        self.assertFalse(_short_evidence({"kind": "speech", "time": 15, "quote": "imposible Graves"}, segment, words, [], hook=True))
        self.assertFalse(_short_evidence({"kind": "speech", "time": 20, "quote": "ganamos la partida"}, segment, words, [], hook=False))
        frames = [{"timestamp": 10}, {"timestamp": 11}]
        self.assertFalse(_short_evidence({"kind": "visual", "time": 11, "detail": "a normal scoreboard"}, segment, words, frames, hook=True))
        self.assertTrue(_short_evidence({"kind": "visual", "before_time": 10, "time": 11, "detail": "the enemy appears in lane"}, segment, words, frames, hook=True))

    def test_payoff_uses_last_evidenced_turn_not_first_kill(self) -> None:
        frames = [{"timestamp": time} for time in (10, 11, 18, 19)]
        evidence = [
            {"kind": "visual", "before_time": 10, "time": 11, "detail": "a single enemy falls"},
            {"kind": "visual", "before_time": 18, "time": 19, "detail": "the whole team is aced"},
        ]
        result = _resolve_evidence(evidence, [], frames, 9, 20, prefer_last=True)
        self.assertEqual(result[0]["time"], 19)

    def test_final_cut_uses_later_prediction_and_self_correction(self) -> None:
        words = [
            {"word": word, "start": timestamp, "end": timestamp + 0.3}
            for timestamp, word in [(236, "Es"), (236.4, "imposible"), (237, "Graves"),
                                    (245, "Oh"), (245.4, "ya"), (245.8, "está"),
                                    (278, "quizá"), (278.4, "tengo"), (278.8, "mal"), (279.2, "tracking")]
        ]
        old = {"title": "Early setup", "source_start": 309, "source_end": 347, "relative_start": 209,
               "relative_end": 247, "segments": [{"source_start": 309, "source_end": 347, "relative_start": 209, "relative_end": 247}]}
        answer = {"title": "The reversal", "story": "Prediction, surprise, admission",
                  "hook": "Graves cannot be here", "payoff": "My tracking was wrong",
                  "hook_evidence": {"kind": "speech", "time": 209, "quote": "Es imposible Graves"},
                  "event_evidence": {"kind": "speech", "quote": "Oh ya está"},
                  "payoff_evidence": {"kind": "speech", "time": 245, "quote": "quizá tengo mal tracking"}}
        cut = _retime_short(old, answer, words, [], 100, (200, 286))
        self.assertIsNotNone(cut)
        self.assertAlmostEqual(cut["relative_start"], 235.75)
        self.assertAlmostEqual(cut["relative_end"], 280.5)
        continuation = [*words, {"word": "estaba", "start": 279.5, "end": 280},
                        {"word": "trackeando", "start": 280, "end": 282.8},
                        {"word": "next", "start": 288, "end": 289}]
        continued = _retime_short(old, answer, continuation, [], 100, (200, 292))
        self.assertAlmostEqual(continued["relative_end"], 283.15)
        with self.assertRaisesRegex(ValueError, "evidence"):
            _retime_short(old, {**answer, "payoff_evidence": {"kind": "speech", "quote": "I win the game"}}, words, [], 100, (200, 286))
        with self.assertRaisesRegex(ValueError, "evidence"):
            _retime_short(old, {**answer, "event_evidence": {"kind": "speech", "quote": "oh nobody wins"}}, words, [], 100, (200, 286))

    def test_multi_short_reuses_word_and_exchange_protection(self) -> None:
        candidate = {"segments": [{"relative_start": 0, "relative_end": 12},
                                  {"relative_start": 18, "relative_end": 24}]}
        answer = {"title": "Moment", "story": "Premise and outcome", "hook": "Premise", "payoff": "Outcome",
                  "segments": [{"start": 0.5, "end": 8.5}, {"start": 18, "end": 22.5}],
                  "hook_evidence": {"kind": "visual", "before_time": 0, "time": 1, "detail": "visible opening event"},
                  "payoff_evidence": {"kind": "visual", "before_time": 19, "time": 20, "detail": "visible related outcome"}}
        words = [{"word": "line", "start": start, "end": end}
                 for start, end in ((0, 1), (8, 9), (9.2, 10), (11, 11.2), (22, 23))]
        frames = [{"timestamp": time} for time in (0, 1, 19, 20)]
        cut = _retime_multi_short(candidate, answer, words, frames, 100)
        self.assertIsNotNone(cut)
        self.assertEqual(cut["segments"][0]["relative_start"], 0)
        self.assertAlmostEqual(cut["segments"][0]["relative_end"], 10.1)
        self.assertAlmostEqual(cut["segments"][1]["relative_end"], 23.1)
        echoed = {**answer, "segments": [{"relative_start": item["start"], "relative_end": item["end"]}
                                         for item in answer["segments"]]}
        self.assertEqual(_retime_multi_short(candidate, echoed, words, frames, 100), cut)
        edge_answer = {**answer, "segments": [answer["segments"][0], {"start": 18, "end": 24}]}
        edge_words = [*words, {"word": "next", "start": 23.8, "end": 25}]
        repaired = _retime_multi_short(candidate, edge_answer, edge_words, frames, 100)
        self.assertAlmostEqual(repaired["segments"][1]["relative_end"], 23.1)
        candidate["segments"][1]["relative_end"] = 22.8
        with self.assertRaisesRegex(ValueError, "context"):
            _retime_multi_short(candidate, answer, words, frames, 100)

    def test_vertical_render_uses_reviewed_crops_and_fills_both_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.media.run_ffmpeg") as ffmpeg:
            render_vertical(
                Path("source.mp4"),
                [{"source_start": 0, "source_end": 10}, {"source_start": 30, "source_end": 40}],
                [0.65, 0.75, 0.18, 0.24],
                [0.2, 0, 0.45, 1],
                Path("captions.ass"),
                Path(temporary) / "short.mp4",
                [0.855, 0, 0.145, 0.028],
            )
        command = ffmpeg.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680,setsar=1", filters)
        self.assertIn("concat=n=2:v=1:a=1", filters)
        self.assertIn("[rawv]split=3[face][game][hud]", filters)
        self.assertIn("scale=1080:1240:force_original_aspect_ratio=increase,crop=1080:1240,setsar=1", filters)
        self.assertIn("[hud]crop=iw*0.145:ih*0.028:iw*0.855:ih*0,scale=320:-2:flags=lanczos,setsar=1[hudout]", filters)
        self.assertIn("[gameout][hudout]overlay=x=W-w:y=0[gamehud]", filters)
        self.assertIn("fontsdir=", filters)
        self.assertIn("[rawa]loudnorm,aresample=48000[a]", filters)
        self.assertAlmostEqual(1280 * 0.49 / 720, 1080 / 1240, delta=0.001)

    def test_shared_league_glossary_covers_current_game_terms(self) -> None:
        glossary = json.loads((Path(__file__).parents[1] / "milloclips" / "league_glossary.json").read_text())
        self.assertIn("Fiddlesticks", glossary["champions"])
        self.assertIn("Zaahen", glossary["champions"])
        self.assertIn("Baron Nashor", glossary["objectives_and_map"])
        self.assertIn("Zhonya's Hourglass", glossary["items"])
        self.assertIn("countergank", glossary["community_terms"])
        self.assertIn("back", glossary["priority_terms"])

    def test_duration_is_enforced_in_code(self) -> None:
        beats = [
            {"id": index, "start": index * 20, "end": (index + 1) * 20, "importance": index % 5 + 1, "keep": index % 2 == 0}
            for index in range(60)
        ]
        selected = _enforce_duration(beats, list(range(60)))
        total = sum(beat["end"] - beat["start"] for beat in beats if beat["id"] in selected)
        self.assertGreaterEqual(total, 540)
        self.assertLessEqual(total, 720)
        for start, end in [(0, 400), (400, 800), (800, 1200)]:
            phase_total = sum(
                beat["end"] - beat["start"]
                for beat in beats
                if beat["id"] in selected and start <= beat["start"] < end
            )
            self.assertGreaterEqual(phase_total, 180)
        runs = []
        for beat_id in selected:
            if runs and beat_id == runs[-1][-1] + 1:
                runs[-1].append(beat_id)
            else:
                runs.append([beat_id])
        self.assertLessEqual(max(len(run) for run in runs) * 20, 120)

    def test_duration_does_not_force_unselected_endpoints(self) -> None:
        beats = [
            {"id": index, "start": index * 20, "end": (index + 1) * 20, "importance": 3, "keep": True}
            for index in range(40)
        ]
        selected = _enforce_duration(beats, list(range(1, 31)))
        self.assertNotIn(0, selected)
        self.assertNotIn(39, selected)

    def test_required_result_beat_is_preserved(self) -> None:
        beats = [
            {"id": index, "start": index * 20, "end": (index + 1) * 20, "importance": 3, "keep": True}
            for index in range(40)
        ]
        selected = _enforce_duration(beats, list(range(30)), required_ids={39})
        self.assertIn(39, selected)

    def test_subtitles_split_on_pauses_and_stay_short(self) -> None:
        words = [
            {"word": "Just", "start": 10.0, "end": 10.2},
            {"word": "go", "start": 10.2, "end": 10.4},
            {"word": "bro", "start": 10.4, "end": 10.7},
            {"word": "No", "start": 13.0, "end": 13.2},
            {"word": "puedo", "start": 13.2, "end": 13.5},
            {"word": "creerlo", "start": 13.5, "end": 13.9},
        ]
        blocks = _subtitle_blocks(words, {"relative_start": 9, "relative_end": 15})
        self.assertEqual([block["text"] for block in blocks], ["Just go bro", "No puedo creerlo"])
        self.assertTrue(all(block["end"] - block["start"] < 3 for block in blocks))
        self.assertLessEqual(blocks[0]["end"], blocks[1]["start"])

    def test_subtitles_normalize_zero_duration_whisper_words(self) -> None:
        words = [
            {"word": "matar", "start": 10.0, "end": 10.0},
            {"word": "ahora", "start": 10.0, "end": 10.0},
        ]
        blocks = _subtitle_blocks(words, {"relative_start": 9, "relative_end": 15})
        timed = blocks[0]["words"]
        self.assertLess(timed[0]["start"], timed[0]["end"])
        self.assertLessEqual(timed[0]["end"], timed[1]["start"])
        self.assertLess(timed[1]["start"], timed[1]["end"])

    def test_subtitles_keep_words_with_identical_starts_in_one_valid_block(self) -> None:
        words = [
            {"word": "muerto", "start": 1191.58, "end": 1191.58},
            {"word": "el", "start": 1191.58, "end": 1195.72},
            {"word": "support", "start": 1195.72, "end": 1195.72},
            {"word": "No", "start": 1198.0, "end": 1198.74},
        ]
        candidate = {"relative_start": 1180.65, "relative_end": 1248.33}
        blocks = _subtitle_blocks(words, candidate)

        _validate_caption_blocks(blocks, 67.68)
        self.assertEqual(blocks[0]["text"], "muerto el")

    def test_reviewed_short_captions_replace_raw_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            candidate = {"source_start": 100, "source_end": 110, "relative_start": 20, "relative_end": 30}
            (directory / "transcript.json").write_text(json.dumps({"words": []}))
            (directory / "short-captions.reviewed.json").write_text(
                json.dumps({"source_start": 100, "source_end": 110, "blocks": [{"start": 1, "end": 2, "text": "CORRECTO"}]})
            )
            subtitles = _write_short_subtitles(directory, candidate).read_text()
            self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,Caption", subtitles)
            self.assertIn("Style: Caption,Montserrat Black,96", subtitles)
            self.assertIn(r"\1c&H000000FF&\fs116\fscx101\fscy101\t(0,120,\fscx100\fscy100)}CORRECTO", subtitles)

    def test_multi_scene_captions_follow_edit_not_source_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            segments = [
                {"source_start": 100, "source_end": 110, "relative_start": 0, "relative_end": 10},
                {"source_start": 200, "source_end": 210, "relative_start": 100, "relative_end": 110},
            ]
            (directory / "transcript.json").write_text(json.dumps({"words": [
                {"word": "antes", "start": 1, "end": 2},
                {"word": "despues", "start": 101, "end": 102},
            ]}))
            subtitles = _write_short_subtitles(directory, {"segments": segments}).read_text()
            self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,Caption", subtitles)
            self.assertIn("Dialogue: 0,0:00:11.00,0:00:12.00,Caption", subtitles)
            self.assertNotIn("0:01:41.00", subtitles)

    def test_long_jump_uses_dialogue_captions_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "transcript.json").write_text(json.dumps({"words": [
                {"word": "before", "start": 1, "end": 2},
                {"word": "after", "start": 103, "end": 104},
            ]}))
            candidate = {"segments": [
                {"source_start": 100, "source_end": 110, "relative_start": 0, "relative_end": 10},
                {"source_start": 200, "source_end": 210, "relative_start": 100, "relative_end": 110},
            ]}
            subtitles = _write_short_subtitles(directory, candidate).read_text()
            self.assertNotIn("Style: Editor", subtitles)
            self.assertNotIn("min después", subtitles)
            self.assertIn("Dialogue: 0,0:00:13.00,0:00:14.00,Caption", subtitles)

    def test_stale_reviewed_candidates_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 200}
            source = directory / "source.mp4"
            source.write_bytes(b"source")
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            (directory / "manifest.json").write_text(json.dumps({"source": str(source), "duration": 200}))
            stat = source.stat()
            (directory / "short-candidates.json").write_text(json.dumps({"version": 8, "output_version": 21, "evidence_version": 7,
                                                                            "cuts_version": 10, "callback_version": 2, "exclusions": [], "game_id": "game-001",
                                                                            "gameplay_start": 100, "editorial_end": 200,
                                                                            "transcript_edits": {"transcript.reviewed.json": None,
                                                                                "short-transcript.json": None, "short-transcript.reviewed.json": None}, "source_identity": {
                                                                                "path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                                                                            "candidates": []}))
            (directory / "short-candidates.reviewed.json").write_text(json.dumps({"game_id": "game-001", "candidates": []}))
            with self.assertRaises(SystemExit):
                short_candidates(directory)

    def test_reviewed_short_cannot_extend_past_selected_game(self) -> None:
        candidate = {"source_start": 190, "source_end": 210, "relative_start": 90, "relative_end": 110,
                     "duration": 20, "segments": [{"source_start": 190, "source_end": 210,
                                                     "relative_start": 90, "relative_end": 110}]}
        with self.assertRaises(ValueError):
            _validate_short_candidates([candidate], 100, 200, 300)

    def test_short_refinement_makes_one_premise_preserving_request(self) -> None:
        ai = AI(Path("."), mock=True)
        ai.mock = False
        candidate = {"title": "Original rant", "reason": "same subject", "hook": "hook", "payoff": "payoff",
                     "source_start": 6510, "source_end": 6525,
                     "segments": [{"relative_start": 10, "relative_end": 25, "source_start": 6510, "source_end": 6525}]}
        with patch.object(ai, "_json", return_value={"reject": True}) as request:
            ai.refine_short(candidate, [], [])
        self.assertEqual(request.call_count, 1)
        self.assertIn("Keep the SAME storyline", request.call_args.args[0])
        self.assertNotIn("6510", request.call_args.args[0])
        self.assertIn('"start": 10, "end": 25', request.call_args.args[0])

    def test_short_refinement_accepts_evidence_nested_in_segment(self) -> None:
        words = [
            {"word": "somos", "start": 10.0, "end": 10.4},
            {"word": "malos", "start": 10.5, "end": 11.0},
            {"word": "estamos", "start": 20.0, "end": 20.5},
            {"word": "trolling", "start": 20.6, "end": 21.2},
        ]
        answer = {"title": "Trolling", "story": "self-own", "hook": "somos malos", "payoff": "estamos trolling",
                  "segments": [{"start": 10, "end": 22,
                                "hook_evidence": {"kind": "speech", "time": 10, "quote": "somos malos"},
                                "payoff_evidence": {"kind": "speech", "time": 20, "quote": "estamos trolling"}}]}
        result = _retime_short({"segments": [{"relative_start": 8, "relative_end": 24}]}, answer, words, [], 100, (8, 24))
        self.assertIsNotNone(result)

    def test_short_render_writes_matching_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            source.write_bytes(b"source")
            subtitles = directory / "short-01.ass"
            subtitles.write_text("captions")
            caption_review = directory / "short-01.captions.reviewed.json"
            caption_review.write_text("{}")
            candidate = {
                "title": "Test", "render_name": "short-01", "output_number": 1,
                "source_start": 10, "source_end": 20, "relative_start": 0, "relative_end": 10,
                "segments": [{"source_start": 10, "source_end": 20, "relative_start": 0, "relative_end": 10}],
            }
            (directory / "manifest.json").write_text(json.dumps({"source": str(source), "duration": 100}))
            game = {"id": "game-001", "gameplay_start": 10, "gameplay_end": 100}
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            (directory / "short-candidates.json").write_text(json.dumps({"version": 8, "output_version": 21, "evidence_version": 7,
                                                                            "cuts_version": 10, "callback_version": 2, "exclusions": [], "game_id": "game-001",
                                                                            "gameplay_start": 10, "editorial_end": 100,
                                                                            "transcript_edits": {"transcript.reviewed.json": None,
                                                                                "short-transcript.json": None, "short-transcript.reviewed.json": None},
                                                                            "candidates": [candidate]}))
            (directory / "layout.json").write_text(json.dumps({"facecam": [0, 0, 0.25, 0.25]}))
            stat = source.stat()
            raw = json.loads((directory / "short-candidates.json").read_text())
            raw["source_identity"] = {"path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            (directory / "short-candidates.json").write_text(json.dumps(raw))
            def fake_render(*args: object) -> None:
                output = args[5]
                output.parent.mkdir(parents=True)
                output.write_bytes(b"video")
            with patch("milloclips.editing._write_short_subtitles", return_value=subtitles), \
                     patch("milloclips.editing.render_vertical", side_effect=fake_render) as render, \
                    patch("milloclips.editing.media_info", return_value={"duration": 10, "width": 1080, "height": 1920, "audio": True}):
                output = render_short(directory)
            self.assertEqual(render.call_args.args[3], [0.22, 0, 0.49, 1])
            provenance = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(provenance["expected_duration"], 10)
            self.assertEqual(provenance["width"], 1080)
            self.assertTrue(provenance["audio"])
            self.assertIsNotNone(provenance["caption_review"])
            self.assertIn("validated render", shorts_report(directory).read_text())
            centered_layout = {"facecam": [0, 0, 0.25, 0.25], "gameplay": [0.255, 0, 0.49, 1]}
            (directory / "layout.reviewed.json").write_text(json.dumps(centered_layout))
            self.assertIn("render missing or stale", shorts_report(directory).read_text())
            (directory / "layout.reviewed.json").unlink()
            self.assertIn("validated render", shorts_report(directory).read_text())
            raw_caption = directory / "short-01.captions.raw.json"
            raw_caption.write_text('{"answer":{"text":"New punctuation!"}}')
            self.assertIn("render missing or stale", shorts_report(directory).read_text())
            raw_caption.unlink()
            self.assertIn("validated render", shorts_report(directory).read_text())
            caption_review.write_text('{"changed": true}')
            self.assertIn("render missing or stale", shorts_report(directory).read_text())

    def test_visual_requests_label_each_image_at_its_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = [root / "first.jpg", root / "second.jpg"]
            for image in images:
                image.write_bytes(b"image")
            response = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
            request = Mock(return_value=response)
            ai = AI(root, mock=True)
            ai.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
            ai._json("Return JSON", images, image_labels=["Frame 0 at 10s", "Frame 1 at 20s"])
            content = request.call_args.kwargs["messages"][0]["content"]
            self.assertEqual([item["type"] for item in content], ["text", "text", "image_url", "text", "image_url"])
            self.assertEqual(content[1]["text"], "Frame 0 at 10s")
            self.assertEqual(content[3]["text"], "Frame 1 at 20s")

    def test_caption_cleanup_preserves_english_words_and_timing_and_restores_closing_marks(self) -> None:
        class FakeAI:
            model = "gpt-5-nano"
            calls = 0

            def punctuate_captions(self, words):
                self.calls += 1
                return {"words": ["Por", "qué?", "Oh", "my", "God!"]}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            words = [{"word": text, "start": index, "end": index + 0.5}
                     for index, text in enumerate(("¿Por", "qué", "¡Oh", "my", "God"))]
            transcript = json.dumps({"words": words})
            (root / "transcript.json").write_text(transcript)
            candidate = {"render_name": "short-01", "segments": [{"relative_start": 0, "relative_end": 8,
                         "source_start": 100, "source_end": 108}]}
            ai = FakeAI()
            cleaned = _short_caption_words(root, candidate, ai)
            self.assertEqual([word["word"] for word in cleaned], ["Por", "qué?", "Oh", "my", "God!"])
            self.assertEqual([(word["start"], word["end"]) for word in cleaned], [(word["start"], word["end"]) for word in words])
            self.assertEqual(_short_caption_words(root, candidate, ai), cleaned)
            self.assertEqual(ai.calls, 1)
            subtitles = _write_short_subtitles(root, candidate).read_text()
            self.assertIn("QUÉ?", subtitles)
            self.assertIn("GOD", subtitles)
            self.assertNotIn("GOD!", subtitles)
            self.assertNotIn("¿", subtitles)
            self.assertEqual((root / "transcript.json").read_text(), transcript)
            ai.model = "changed-model"
            ai.punctuate_captions = lambda _words: {"words": ["Por", "qué?", "Oh", "my", "Dios!"]}
            preserved = _short_caption_words(root, candidate, ai)
            self.assertEqual(preserved[-1]["word"], "God!")
            self.assertNotIn("Dios", " ".join(word["word"] for word in preserved))

    def test_ass_captions_highlight_words_at_their_spoken_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            candidate = {"source_start": 100, "source_end": 110, "relative_start": 20, "relative_end": 30}
            transcript = {
                "words": [
                    {"word": "Baron", "start": 21.0, "end": 21.4},
                    {"word": "ahora", "start": 21.4, "end": 22.0},
                ]
            }
            (directory / "transcript.json").write_text(json.dumps(transcript))
            subtitles = _write_short_subtitles(directory, candidate).read_text()
            self.assertIn("Dialogue: 0,0:00:01.00,0:00:01.40,Caption", subtitles)
            self.assertIn(r"\1c&H000000FF&\fs116", subtitles)
            self.assertIn(r"\t(0,120,\fscx100\fscy100)", subtitles)
            centers = {}
            for word in ("BARON", "AHORA"):
                positions = re.findall(r"\\move\(([^)]+)\)[^\n]*?}" + word, subtitles)
                self.assertEqual(len(positions), 2)
                centers[word] = []
                for position in positions:
                    start_x, start_y, end_x, end_y, *_ = map(float, position.split(","))
                    self.assertEqual(start_y, end_y)
                    self.assertEqual(end_y, 652)
                    self.assertGreater(abs(start_x - 540), abs(end_x - 540))
                    centers[word].append(end_x)
            self.assertLess(centers["BARON"][1], centers["BARON"][0])
            self.assertLess(centers["AHORA"][1], centers["AHORA"][0])

    def test_caption_spacing_keeps_room_around_highlight(self) -> None:
        font = ImageFont.truetype(str(Path(__file__).parents[1] / "milloclips/fonts/Montserrat-Black.ttf"), 60)
        words = [{"text": word} for word in ("si", "el", "fiddlesticks")]
        positions = [
            float(re.search(r"\\move\([^,]+,[^,]+,([^,]+),", text).group(1))
            for text in _ass_text(words, 0, font)
        ]
        widths = [font.getlength(word.upper()) * (116 / 96 if index == 0 else 1) for index, word in enumerate(("si", "el", "fiddlesticks"))]
        short_gap = positions[1] - positions[0] - (widths[0] + widths[1]) / 2
        long_gap = positions[2] - positions[1] - (widths[1] + widths[2]) / 2
        self.assertAlmostEqual(short_gap, font.getlength(" ") + 8, delta=0.1)
        self.assertAlmostEqual(long_gap, font.getlength(" ") + 8, delta=0.1)

    def test_rejects_overlapping_reviewed_caption_words(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            candidate = {"source_start": 100, "source_end": 110, "relative_start": 20, "relative_end": 30}
            (directory / "transcript.json").write_text(json.dumps({"words": []}))
            reviewed = {
                "source_start": 100,
                "source_end": 110,
                "blocks": [{
                    "start": 1,
                    "end": 2,
                    "text": "mal timing",
                    "words": [
                        {"text": "mal", "start": 1, "end": 1.7},
                        {"text": "timing", "start": 1.6, "end": 2},
                    ],
                }],
            }
            (directory / "short-captions.reviewed.json").write_text(json.dumps(reviewed))
            with self.assertRaises(ValueError):
                _write_short_subtitles(directory, candidate)

    def test_beat_intervals_follow_pauses_and_cover_duration(self) -> None:
        words = [
            {"start": 0, "end": 4},
            {"start": 4.2, "end": 10},
            {"start": 13, "end": 18},
            {"start": 18.2, "end": 27},
            {"start": 31, "end": 42},
        ]
        intervals = _beat_intervals(50, words)
        self.assertEqual(intervals[0][0], 0)
        self.assertEqual(intervals[-1][1], 50)
        self.assertTrue(all(8 <= end - start <= 25 for start, end in intervals))
        self.assertTrue(any(end - start != 15 for start, end in intervals))

    def test_reviewed_transcript_excludes_bad_ranges_without_mutating_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw = {
                "words": [
                    {"word": "hallucination", "start": 10, "end": 11},
                    {"word": "useful", "start": 30, "end": 31},
                ]
            }
            (directory / "transcript.json").write_text(json.dumps(raw))
            (directory / "transcript.reviewed.json").write_text(
                json.dumps({
                    "exclude_ranges": [{"start": 0, "end": 20}],
                    "replacements": [{"start": 30, "end": 31, "text": "injugable"}],
                })
            )
            self.assertEqual([word["word"] for word in transcript_words(directory)], ["injugable"])
            self.assertEqual(json.loads((directory / "transcript.json").read_text()), raw)

    def test_local_transcription_restores_speech_after_excluded_whisper_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw = {"words": [{"word": "before", "start": 1, "end": 1.5},
                             {"word": "A", "start": 101, "end": 101.5}]}
            (directory / "transcript.json").write_text(json.dumps(raw))
            (directory / "transcript.reviewed.json").write_text(json.dumps({"exclude_ranges": [{"start": 100, "end": 110}]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-001"}))
            (directory / "short-transcript.json").write_text(json.dumps({"game_id": "game-001", "repairs": [{
                "relative_start": 100, "relative_end": 110,
                "words": [{"word": "Tristana", "start": 1, "end": 1.5},
                          {"word": "Amara.org", "start": 9, "end": 9.4}],
            }]}))
            (directory / "short-transcript.reviewed.json").write_text(json.dumps({"exclude_ranges": [{"start": 108.8, "end": 110}]}))
            self.assertEqual([word["word"] for word in transcript_words(directory)], ["before", "Tristana"])
            self.assertEqual(json.loads((directory / "transcript.json").read_text()), raw)

    def test_reviewed_games_override_raw_without_mutating_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw = {"games": [{"id": "game-001", "gameplay_start": 10}]}
            reviewed = {"games": [{"id": "game-001", "gameplay_start": 12}]}
            (directory / "games.json").write_text(json.dumps(raw))
            (directory / "games.reviewed.json").write_text(json.dumps(reviewed))
            game = select_game(directory, "game-001")
            self.assertEqual(game["gameplay_start"], 12)
            self.assertEqual(json.loads((directory / "games.json").read_text()), raw)


if __name__ == "__main__":
    unittest.main()
