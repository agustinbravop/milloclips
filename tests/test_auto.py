from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from milloclips.cli import _auto
from milloclips.io import read_json


GAME = {"id": "game-001", "champ_select_start": None, "loading_start": None,
        "gameplay_start": 0.0, "gameplay_end": 1200.0, "postgame_end": 1200.0, "duration": 1200.0}
BEAT = {"id": 0, "start": 0.0, "end": 15.0, "phase": "IN_GAME", "type": "COMBAT", "importance": 4, "keep": True}


class FakeAI:
    def __init__(self, vod_dir: Path, model: str = "gpt-5-nano", editorial_model: str | None = None, mock: bool = True):
        self.vod_dir = vod_dir
        self.model = model
        self.editorial_model = editorial_model or model
        self.mock = mock

    def rank_games(self, analyses):
        return [{"game_id": item["game_id"], "score": 90 - index, "reason": "synthetic"}
                for index, item in enumerate(analyses)]


def _workspace(root: Path) -> Path:
    vod_dir = root / "abc123abc123"
    vod_dir.mkdir(parents=True)
    (vod_dir / "manifest.json").write_text(json.dumps(
        {"vod_id": "abc123abc123", "source": str(root / "source.mp4"), "duration": 1200.0,
         "metadata": {"kind": "kick", "input": "https://kick.com/elmiillor/videos/x"}}))
    (vod_dir / "games.json").write_text(json.dumps({"games": [GAME]}))
    (vod_dir / "stream-states.json").write_text(json.dumps({"points": [{"timestamp": 0.0, "state": "IN_GAME", "confidence": 1.0}]}))
    return vod_dir


def _arguments(source: str, root: Path) -> argparse.Namespace:
    return argparse.Namespace(source=source, root=root, force=False, model="gpt-5-nano", editorial_model=None)


class AutoOrchestrationTests(unittest.TestCase):
    """`auto` must record a publishable workspace and a status.json a batch driver can read."""

    def _run(self, root: Path, vod_dir: Path) -> None:
        winner_dir = vod_dir / "game-analyses" / "game-001"
        winner_dir.mkdir(parents=True)
        with patch("milloclips.cli.AI", FakeAI), \
             patch("milloclips.cli.scan", return_value=[GAME]), \
             patch("milloclips.cli.analyze", return_value=[BEAT]), \
             patch("milloclips.cli.plan_long", return_value={}), \
             patch("milloclips.cli.render_long", return_value=winner_dir / "renders" / "long-rough.mp4"), \
             patch("milloclips.cli.find_vod_shorts", return_value=[{"title": "One"}]), \
             patch("milloclips.cli.render_shorts", return_value=[]), \
             patch("milloclips.cli.render_thumbnails", return_value=winner_dir / "renders" / "thumbnails"), \
             patch("milloclips.cli.scan_report", return_value=vod_dir / "reports" / "scan-report.html"), \
             patch("milloclips.cli.shorts_report", return_value=vod_dir / "reports" / "short-candidates.html"):
            _auto(_arguments(str(vod_dir), root))

    def test_summary_records_the_workspace_publish_expects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod_dir = _workspace(root)
            self._run(root, vod_dir)
            summary = read_json(vod_dir / "auto-summary.json")
            winner_dir = vod_dir / "game-analyses" / "game-001"
            # publish reads renders/long-rough.mp4 relative to the workspace it is given.
            self.assertEqual(summary["workspace"], str(winner_dir))
            self.assertEqual(summary["source_workspace"], str(vod_dir))
            self.assertEqual(summary["selected_game"], "game-001")
            self.assertEqual(summary["long_video"], str(winner_dir / "renders" / "long-rough.mp4"))
            self.assertEqual(summary["thumbnail"], str(winner_dir / "renders" / "thumbnail.png"))

    def test_status_records_a_finished_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod_dir = _workspace(root)
            self._run(root, vod_dir)
            status = read_json(vod_dir / "status.json")
            self.assertEqual(status["stage"], "done")
            self.assertTrue(status["ok"])
            self.assertIsNone(status["error"])
            self.assertEqual(status["source_workspace"], str(vod_dir))

    def test_status_records_the_failing_stage_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod_dir = _workspace(root)
            winner_dir = vod_dir / "game-analyses" / "game-001"
            winner_dir.mkdir(parents=True)
            with patch("milloclips.cli.AI", FakeAI), \
                 patch("milloclips.cli.scan", return_value=[GAME]), \
                 patch("milloclips.cli.analyze", return_value=[BEAT]), \
                 patch("milloclips.cli.plan_long", side_effect=ValueError("Intro needs review after one setup retry")), \
                 patch("milloclips.cli.scan_report", return_value=vod_dir / "reports" / "scan-report.html"):
                with self.assertRaises(ValueError):
                    _auto(_arguments(str(vod_dir), root))
            status = read_json(vod_dir / "status.json")
            self.assertEqual(status["stage"], "plan-long")
            self.assertFalse(status["ok"])
            self.assertIn("Intro needs review", status["error"])
            self.assertFalse((vod_dir / "auto-summary.json").exists())

    def test_status_records_which_game_failed_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod_dir = _workspace(root)
            with patch("milloclips.cli.AI", FakeAI), \
                 patch("milloclips.cli.scan", return_value=[GAME]), \
                 patch("milloclips.cli.analyze", side_effect=RuntimeError("429 rate limited")), \
                 patch("milloclips.cli.scan_report", return_value=vod_dir / "reports" / "scan-report.html"):
                with self.assertRaises(RuntimeError):
                    _auto(_arguments(str(vod_dir), root))
            status = read_json(vod_dir / "status.json")
            self.assertEqual(status["stage"], "analyze:game-001")
            self.assertFalse(status["ok"])

    def test_no_eligible_games_is_a_recorded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod_dir = _workspace(root)
            (vod_dir / "games.json").write_text(json.dumps({"games": []}))
            with patch("milloclips.cli.AI", FakeAI), \
                 patch("milloclips.cli.scan", return_value=[]), \
                 patch("milloclips.cli.scan_report", return_value=vod_dir / "reports" / "scan-report.html"):
                with self.assertRaises(SystemExit):
                    _auto(_arguments(str(vod_dir), root))
            status = read_json(vod_dir / "status.json")
            self.assertEqual(status["stage"], "scan")
            self.assertFalse(status["ok"])


if __name__ == "__main__":
    unittest.main()
