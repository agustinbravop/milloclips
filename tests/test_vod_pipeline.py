from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from milloclips.ai import AI
from milloclips.editing import _check_short_moments, _file_fingerprint, short_candidates, _short_source_words, _short_layout
from milloclips.io import read_json, write_json
from milloclips.pipeline import _offset_event, _vod_regions, find_vod_shorts, rank_vod_games, prepare_stream_workspace


class VodPipelineTests(unittest.TestCase):
    def test_short_layout_follows_source_region_and_preserves_reviewed_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence = root / "vod-shorts"
            first = {"facecam": [.7, .05, .25, .25]}
            second = {"facecam": [.65, .75, .19, .24]}
            write_json(root / "layout.json", first)
            write_json(evidence / "layout.json", first)
            write_json(root / "short-candidates.json", {"scope": "vod", "workspace": str(evidence)})
            write_json(evidence / "short-candidates.json", {"game_id": "vod"})
            write_json(evidence / "beats.json", {"coverage": [
                {"region": "game-001", "start": 0, "end": 100},
                {"region": "game-002", "start": 100, "end": 200}]})
            region = root / "game-analyses/game-002"
            write_json(region / "layout.json", second)
            candidate = {"source_start": 120}
            self.assertEqual(_short_layout(root, candidate)["facecam"], second["facecam"])
            self.assertEqual(_short_layout(evidence, candidate)["facecam"], second["facecam"])
            self.assertEqual(_short_layout(root, {"source_start": 20})["facecam"], first["facecam"])
            write_json(region / "layout.reviewed.json", first)
            self.assertEqual(_short_layout(root, candidate)["facecam"], first["facecam"])
            write_json(root / "layout.reviewed.json", second)
            self.assertEqual(_short_layout(root, candidate)["facecam"], second["facecam"])

    def test_stream_promotion_preserves_all_games_and_does_not_overwrite_newer_plans(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            detected = self.workspace(root)
            directory = root / "game-analyses/game-002"
            (directory / "renders").mkdir(parents=True)
            write_json(directory / "beats.json", {"game_id": "game-002", "beats": []})
            write_json(directory / "long-plan.json", {"story": "cached analysis"})
            (directory / "renders/long-rough.mp4").write_bytes(b"original upload")
            self.assertEqual(prepare_stream_workspace(root, directory), root)
            self.assertEqual(read_json(root / "games.json")["games"], detected)
            self.assertEqual(read_json(root / "beats.json")["game_id"], "game-002")
            write_json(root / "long-plan.json", {"story": "new approved edit"})
            (root / "renders/long-rough.mp4").write_bytes(b"new render")
            prepare_stream_workspace(root, directory)
            self.assertEqual(read_json(root / "long-plan.json")["story"], "new approved edit")
            self.assertEqual((directory / "renders/long-rough.mp4").read_bytes(), b"original upload")

    def test_short_cannot_lose_its_payoff_during_boundary_construction(self):
        with self.assertRaisesRegex(ValueError, "payoff"):
            _check_short_moments([{"start": 1612, "end": 1652}], [1613, 1655])
        _check_short_moments([{"start": 1612, "end": 1657}], [1613, 1655])

    def workspace(self, root):
        source = root / "source.mp4"
        source.touch()
        write_json(root / "manifest.json", {"source": str(source), "duration": 1800})
        detected = [{"id": f"game-{index:03d}", "gameplay_start": start, "gameplay_end": end,
                     "duration": end - start, "champ_select_start": None, "postgame_end": end}
                    for index, (start, end) in enumerate([(650, 900), (1000, 1600)], 1)]
        write_json(root / "games.json", {"games": detected})
        return detected

    def test_regions_cover_opening_between_games_and_tail_without_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.workspace(root)
            regions = _vod_regions(root)
            self.assertEqual(regions[0]["gameplay_start"], 0)
            self.assertEqual(regions[-1]["gameplay_end"], 1800)
            self.assertTrue(all(left["gameplay_end"] == right["gameplay_start"] for left, right in zip(regions, regions[1:])))
            self.assertEqual(sum(item["duration"] for item in regions), 1800)
            self.assertTrue(any(item["gameplay_start"] == 900 and item["gameplay_end"] == 1000 for item in regions))

    def test_ranking_analyzes_every_game_and_can_choose_later_game(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.workspace(root)
            ai = AI(root, mock=True)
            def beats(directory, *_args, **_kwargs):
                importance = 2 if directory.name == "game-001" else 5
                return [{"importance": importance}]
            with patch("milloclips.pipeline.analyze", side_effect=beats) as analyze:
                ranking, winner = rank_vod_games(root, ai)
                self.assertEqual(analyze.call_count, 2)
                self.assertEqual(winner.name, "game-002")
                self.assertEqual(read_json(root / "selection.json")["game_id"], "game-002")
                with patch.object(ai, "rank_games", side_effect=AssertionError("ranking should be cached")):
                    self.assertEqual(rank_vod_games(root, ai)[0], ranking)

    def test_whole_vod_judge_receives_game_and_non_game_evidence_on_absolute_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.workspace(root)
            ai = AI(root, mock=True)
            def analyze(directory, *_args, **_kwargs):
                region = read_json(directory / "games.json")["games"][0]
                beat = {"id": 0, "start": 0, "end": region["duration"], "type": "REACTION", "importance": 5, "text": region["id"]}
                write_json(directory / "beats.json", {"beats": [beat]})
                write_json(directory / "transcript.json", {"words": [{"word": region["id"], "start": 1, "end": 2}]})
                write_json(directory / "layout.json", {"facecam": [0, 0, .3, .3]})
                return [beat]
            def discover(directory, *_args, **_kwargs):
                self.assertEqual(read_json(directory / "games.json")["games"][0]["league_ranges"],
                                 [{"start": 650, "end": 900}, {"start": 1000, "end": 1600}])
                beats = read_json(directory / "beats.json")["beats"]
                self.assertEqual([item["start"] for item in beats], [0, 600, 650, 900, 1000, 1600])
                self.assertEqual(len({item["id"] for item in beats}), len(beats))
                self.assertIn("context-003", [word["word"] for word in _short_source_words(directory)])
                candidates = [{"render_name": f"short-{index:02d}"} for index in (1, 2)]
                write_json(directory / "short-candidates.json", {"candidates": candidates})
                for item in candidates:
                    write_json(directory / f"{item['render_name']}.captions.raw.json", {})
                return candidates
            with patch("milloclips.pipeline.analyze", side_effect=analyze), \
                 patch("milloclips.pipeline.sample_frames", return_value=[]), \
                 patch("milloclips.pipeline.find_shorts", side_effect=discover) as scout:
                find_vod_shorts(root, ai)
                self.assertEqual(scout.call_count, 1)
                self.assertEqual(read_json(root / "short-candidates.json")["scope"], "vod")
                self.assertEqual([word["start"] for word in _short_source_words(root)], [1, 601, 651, 901, 1001, 1601])

    def test_event_offset_preserves_source_times_and_durations_without_mutating_raw(self):
        raw = {"start": 3, "end": 8, "source_start": 103, "duration": 5,
               "evidence": {"outcome_time": {"kind": "visual", "time": 7}}}
        converted = _offset_event(raw, 100)
        self.assertEqual(converted["start"], 103)
        self.assertEqual(converted["source_start"], 103)
        self.assertEqual(converted["duration"], 5)
        self.assertEqual(converted["evidence"]["outcome_time"]["time"], 107)
        self.assertEqual(raw["start"], 3)

    def test_game_judge_sees_chronology_including_low_priority_ending(self):
        with tempfile.TemporaryDirectory() as folder:
            ai = AI(Path(folder), mock=True)
            ai.mock = False
            beats = [{"id": index, "start": index * 15, "end": (index + 1) * 15,
                      "importance": 5 if index < 40 else 1, "keep": index < 40, "text": str(index)} for index in range(100)]
            with patch.object(ai, "_json", return_value={"games": [{"game_id": "game-001", "score": 80, "reason": "arc"}]}) as request:
                ai.rank_games([{"game_id": "game-001", "duration": 1500, "beats": beats}])
                prompt = request.call_args.args[0]
                import json
                evidence = json.loads(prompt.split("Evidence: ")[1])[0]["beats"]
                self.assertLessEqual(len(evidence), 30)
                self.assertEqual(evidence[-1]["text"], "99")
                self.assertTrue(any(600 < beat["start"] < 1000 for beat in evidence))

    def test_linked_shorts_reject_changed_global_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence = root / "vod-shorts"
            evidence.mkdir()
            write_json(evidence / "short-candidates.json", {"candidates": []})
            write_json(root / "short-candidates.json", {"scope": "vod", "workspace": str(evidence),
                       "signature": _file_fingerprint(evidence / "short-candidates.json")})
            write_json(evidence / "short-candidates.json", {"candidates": [{"title": "changed"}]})
            with self.assertRaisesRegex(SystemExit, "changed"):
                short_candidates(root)

    def test_short_windows_resume_without_repeating_completed_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            ai = AI(Path(folder), mock=True)
            ai.mock = False
            beats = [{"id": index, "start": index * 15, "end": (index + 1) * 15,
                      "importance": 5, "type": "REACTION", "keep": True, "text": str(index)} for index in range(25)]
            with patch.object(ai, "_json", return_value={"candidates": []}) as request:
                self.assertEqual(ai.shorts(beats), [])
                self.assertEqual(request.call_count, 1)
                request.reset_mock()
                self.assertEqual(ai.shorts(beats), [])
                self.assertEqual(request.call_count, 0)  # No uncached wide/callback pass.
