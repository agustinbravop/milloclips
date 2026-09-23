from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from milloclips.editing import _beat_intervals, _enforce_duration, _subtitle_blocks, transcript_words, select_game
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
                json.dumps({"exclude_ranges": [{"start": 0, "end": 20}]})
            )
            self.assertEqual([word["word"] for word in transcript_words(directory)], ["useful"])
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
