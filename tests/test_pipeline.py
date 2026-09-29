from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from milloclips.editing import (
    _ass_text,
    _beat_intervals,
    _enforce_duration,
    _resolve_evidence,
    _retime_short,
    _short_evidence,
    _subtitle_blocks,
    _validate_caption_blocks,
    _write_short_subtitles,
    analyze,
    find_shorts,
    select_game,
    transcript_words,
)
from milloclips.media import render_vertical
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


class EditingTests(unittest.TestCase):
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

            def shorts(self, beats: list[dict]) -> list[dict]:
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
            cache_key = {"game_id": "game-002", "gameplay_start": 100, "editorial_end": 240}
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
            self.assertEqual(candidates[0]["duration"], 49)
            self.assertEqual([segment["source_start"] for segment in candidates[0]["segments"]], [101, 142, 182])
            self.assertEqual(json.loads((directory / "short-proposals.json").read_text())["version"], 7)
            self.assertEqual(len(ai.seen_candidates), 1)
            self.assertEqual(find_shorts(directory, ai), candidates)
            ai.choose_shorts = lambda _candidates, _images: []
            self.assertEqual(find_shorts(directory, ai, force=True), [])
            (directory / "short-exclusions.reviewed.json").write_text(json.dumps({"game_id": "game-002", "ranges": [{"start": 0, "end": 20}]}))
            self.assertEqual(find_shorts(directory, ai), [])
            self.assertEqual(len(ai.seen), len(beats) - 2)

    def test_visual_callback_reaches_verification_despite_corrupt_late_transcript(self) -> None:
        class FakeAI:
            mock = True

            def shorts(self, _beats: list[dict]) -> list[dict]:
                return []

            def visual_shorts(self, _frames: list[dict]) -> list[dict]:
                return []

            def callback_shorts(self, _beats: list[dict], _frames: list[dict]) -> list[dict]:
                return [{"title": "The teammate proves him wrong", "payoff_beat_id": 2,
                         "proposed_payoff": "The teammate survives while the streamer is dead",
                         "scenes": [{"start_beat_id": 0, "end_beat_id": 0},
                                    {"start_beat_id": 14, "end_beat_id": 14},
                                    {"start_beat_id": 15, "end_beat_id": 15}]}]

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
            (directory / "beats.json").write_text(json.dumps({"game_id": "game-001", "gameplay_start": 100,
                                                                 "editorial_end": 280, "beats": beats}))
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
            self.assertEqual(json.loads((directory / "short-callback-proposals.json").read_text())["callback_version"], 1)

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
        self.assertIsNone(_retime_short(old, {**answer, "payoff_evidence": {"kind": "speech", "quote": "I win the game"}}, words, [], 100, (200, 286)))
        self.assertIsNone(_retime_short(old, {**answer, "event_evidence": {"kind": "speech", "quote": "oh nobody wins"}}, words, [], 100, (200, 286)))

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
        self.assertAlmostEqual(1280 * 0.49 / 720, 1080 / 1240, delta=0.001)
        self.assertAlmostEqual(0.255 + 0.49 / 2, 0.5)

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

    def test_long_jump_gets_distinct_centered_editor_label(self) -> None:
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
            self.assertIn("Style: Editor,Montserrat Black,78,&H00FFA95B&", subtitles)
            self.assertIn(r"\an5\pos(540,960)}2 min después", subtitles)
            self.assertIn("Dialogue: 0,0:00:13.00,0:00:14.00,Caption", subtitles)

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
