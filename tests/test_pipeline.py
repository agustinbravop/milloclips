from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from milloclips.editing import (
    _beat_intervals,
    _enforce_duration,
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

    def test_short_selection_uses_all_beats_and_normalizes_candidates(self) -> None:
        class FakeAI:
            seen: list[dict] = []

            def shorts(self, beats: list[dict]) -> list[dict]:
                self.seen = beats
                candidates = [
                    {
                        "start_beat_id": index * 2,
                        "end_beat_id": index * 2 + 1,
                        "title": f"Moment {index}",
                        "reason": "Complete moment",
                        "scores": {"hook": 5 - index / 10, "context": 4, "payoff": 5, "tightness": 4},
                    }
                    for index in range(12)
                ]
                candidates.extend([
                    {
                        "start_beat_id": 1,
                        "end_beat_id": 2,
                        "title": "Overlapping",
                        "scores": {"hook": 4.4, "context": 4.4, "payoff": 4.4, "tightness": 4.4},
                    },
                    {
                        "start_beat_id": 24,
                        "end_beat_id": 24,
                        "title": "Too short",
                        "scores": {"hook": 5, "context": 5, "payoff": 5, "tightness": 5},
                    },
                    {
                        "start_beat_id": 24,
                        "end_beat_id": 25,
                        "title": "Invalid score",
                        "scores": {"hook": float("nan"), "context": 5, "payoff": 5, "tightness": 5},
                    },
                ])
                return candidates

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            game = {"id": "game-002", "gameplay_start": 100, "gameplay_end": 400}
            cache_key = {"game_id": "game-002", "gameplay_start": 100, "editorial_end": 400}
            beats = [
                {
                    "id": index,
                    "start": index * 10,
                    "end": index * 10 + (5 if index == 24 else 10),
                    "phase": "IN_GAME",
                    "type": "FARMING" if index == 0 else "COMBAT",
                    "importance": 1 if index == 0 else 4,
                    "keep": index != 0,
                    "text": "context",
                    "reason": "test",
                }
                for index in range(26)
            ]
            (directory / "games.json").write_text(json.dumps({"games": [game]}))
            (directory / "selection.json").write_text(json.dumps({"game_id": "game-002"}))
            (directory / "beats.json").write_text(json.dumps({**cache_key, "beats": beats}))
            (directory / "short-candidates.json").write_text(json.dumps({"game_id": "game-001", "candidates": []}))
            ai = FakeAI()

            candidates = find_shorts(directory, ai)

            self.assertEqual(len(ai.seen), len(beats))
            self.assertEqual(ai.seen[0]["importance"], 1)
            self.assertEqual(len(candidates), 10)
            self.assertEqual(candidates[0]["start_beat_id"], 0)
            self.assertEqual(candidates[0]["source_start"], 100)
            self.assertEqual(candidates[0]["score"], 4.5)
            self.assertNotIn("Overlapping", [candidate["title"] for candidate in candidates])
            self.assertEqual(json.loads((directory / "short-candidates.json").read_text())["game_id"], "game-002")

    def test_vertical_render_preserves_crop_aspect_ratio_and_prioritizes_gameplay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("milloclips.media.run_ffmpeg") as ffmpeg:
            render_vertical(
                Path("source.mp4"),
                0,
                10,
                [0.65, 0.75, 0.18, 0.24],
                [0.2, 0, 0.45, 1],
                Path("captions.ass"),
                Path(temporary) / "short.mp4",
            )
        command = ffmpeg.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:620:force_original_aspect_ratio=increase,crop=1080:620,setsar=1", filters)
        self.assertIn("scale=1080:1300:force_original_aspect_ratio=increase,crop=1080:1300,setsar=1", filters)

    def test_shared_league_glossary_covers_current_game_terms(self) -> None:
        glossary = json.loads((Path(__file__).parents[1] / "milloclips" / "league_glossary.json").read_text())
        self.assertIn("Fiddlesticks", glossary["champions"])
        self.assertIn("Zaahen", glossary["champions"])
        self.assertIn("Baron Nashor", glossary["objectives_and_map"])
        self.assertIn("Zhonya's Hourglass", glossary["items"])
        self.assertIn("countergank", glossary["community_terms"])

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
            self.assertIn(r"{\an5\pos(540,620)}{\1c&H000000FF&\fs80}CORRECTO", subtitles)

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
            self.assertIn(r"{\1c&H000000FF&\fs80}BARON {\1c&H00FFFFFF&\fs72}AHORA", subtitles)
            self.assertIn(r"{\1c&H00FFFFFF&\fs72}BARON {\1c&H000000FF&\fs80}AHORA", subtitles)

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
