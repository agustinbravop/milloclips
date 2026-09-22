from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from milloclips.editing import _enforce_duration, select_game
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
        self.assertLessEqual(total, 660)
        self.assertIn(0, selected)
        self.assertIn(59, selected)

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
