import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from milloclips.ai import AI
from milloclips.editing import _deduplicate_long_events, _long_boundary_segments, _long_scene_ranges, _refine_long_scenes, plan_long


def event(start=10, end=30, *, action=12, outcome=26, reaction=28, downtime=30):
    return {"start": start, "end": end, "action_start": action, "outcome_time": outcome, "reaction_end": reaction,
            "downtime_start": downtime, "downtime_kind": "recall" if downtime is not None else "none",
            "event_kind": "combat", "start_reason": "Approach before engagement", "end_reason": "Recall after reaction",
            "evidence": {key: {"kind": "visual", "time": time, "detail": "Visible event at this inspected frame"}
                         for key, time in {"action_start": action, "outcome_time": outcome, "reaction_end": reaction,
                                           **({"downtime_start": downtime} if downtime is not None else {})}.items()}}


def decision(*parts):
    return {"needs_earlier_context": False, "needs_later_context": False, "segments": list(parts)}


class BoundaryTests(unittest.TestCase):
    def test_shared_setup_is_deduplicated_without_altering_approved_bounds(self):
        narrow = {**event(), "source_start": 10, "source_end": 30, "scene_id": "first", "required": False}
        wide = {**event(9, 40), "source_start": 9, "source_end": 40, "scene_id": "second", "required": True}
        result = _deduplicate_long_events([narrow, wide])
        self.assertEqual([(x["source_start"], x["source_end"]) for x in result], [(9, 40)])
        self.assertEqual(set(result[0]["scene_ids"]), {"first", "second"})
        self.assertTrue(result[0]["required"])
        wide["source_start"] = 15
        with self.assertRaisesRegex(ValueError, "overlap"):
            _deduplicate_long_events([narrow, wide])

    def test_one_word_speech_anchor_must_match_its_word_timestamp(self):
        part = event()
        part["evidence"]["outcome_time"] = {"kind": "speech", "time": 26, "quote": "chat"}
        words = [{"word": "chat", "start": 26, "end": 26.3}]
        _long_boundary_segments(decision(part), (0, 50), words, [{"timestamp": t} for t in range(51)])
        words[0]["start"] = 24
        with self.assertRaisesRegex(ValueError, "Unmatched speech anchor"):
            _long_boundary_segments(decision(part), (0, 50), words, [{"timestamp": t} for t in range(51)])

    def test_recall_onset_removes_channel_and_return_travel(self):
        answer = decision(event())
        frames = [{"timestamp": time} for time in range(50)]
        parts = _long_boundary_segments(answer, (0, 50), [], frames)
        self.assertEqual((parts[0]["start"], parts[0]["end"]), (10, 30))
        answer["segments"][0]["end"] = 40
        with self.assertRaisesRegex(ValueError, "downtime"):
            _long_boundary_segments(answer, (0, 50), [], frames)

    def test_complete_spoken_reaction_can_continue_during_recall(self):
        part = event(end=32, reaction=28, downtime=30)
        part["evidence"]["reaction_end"] = {"kind": "speech", "time": 28, "quote": "muy buena pelea"}
        words = [{"word": "muy", "start": 18, "end": 18.3}, {"word": "buena", "start": 18.3, "end": 19},
                 {"word": "pelea", "start": 19, "end": 19.7},
                 {"word": "muy", "start": 28, "end": 28.3}, {"word": "buena", "start": 28.3, "end": 29},
                 {"word": "pelea", "start": 29, "end": 31.7}]
        parts = _long_boundary_segments(decision(part), (0, 50), words, [{"timestamp": t} for t in range(50)])
        self.assertEqual(parts[0]["end"], 32)
        part["end"] = 30
        repaired = _long_boundary_segments(decision(part), (0, 50), words, [{"timestamp": t} for t in range(50)])
        self.assertAlmostEqual(repaired[0]["end"], 31.8)

    def test_reaction_can_begin_before_result_banner(self):
        part = event(outcome=29, reaction=28)
        _long_boundary_segments(decision(part), (0, 50), [], [{"timestamp": t} for t in range(51)])

    def test_coarse_exit_on_reaction_frame_is_extended_to_retain_it(self):
        part = event(end=28, downtime=None)
        retained = _long_boundary_segments(decision(part), (0, 50), [], [{"timestamp": t} for t in range(51)])
        self.assertAlmostEqual(retained[0]["end"], 28.1)

    def test_two_events_keep_internal_gap_out_of_the_edit(self):
        parts = _long_boundary_segments(decision(event(), event(50, 70, action=52, outcome=66, reaction=68, downtime=70)),
                                        (0, 80), [], [{"timestamp": t} for t in range(81)])
        self.assertEqual(sum(p["end"] - p["start"] for p in parts), 40)
        self.assertEqual(len(parts), 2)

    def test_mid_action_start_and_uninspected_anchor_need_review(self):
        part = event(start=12)
        with self.assertRaisesRegex(ValueError, "pre-action"):
            _long_boundary_segments(decision(part), (0, 50), [], [{"timestamp": t} for t in range(51)])
        part = event()
        with self.assertRaisesRegex(ValueError, "inspected frame"):
            _long_boundary_segments(decision(part), (0, 50), [], [{"timestamp": 0}])

    def test_word_adjustment_cannot_create_overlap_or_leave_inspected_window(self):
        words = [{"word": "una", "start": 29, "end": 30.5}]
        parts = decision(event(), event(30.25, 50, action=33, outcome=46, reaction=48, downtime=50))
        with self.assertRaises(ValueError):
            _long_boundary_segments(parts, (0, 60), words, [], mock=True)
        windows = [{"start": [9, 11], "end": [29, 30]}]
        with self.assertRaisesRegex(ValueError, "window"):
            _long_boundary_segments(decision(event(downtime=None)), (0, 50), words, [], windows=windows, mock=True)

    def test_selection_does_not_pad_merge_adjacent_or_inject_pregame(self):
        beats = [{"id": i, "start": i * 20, "end": (i + 1) * 20, "phase": "IN_GAME", "importance": 5}
                 for i in range(30)]
        scenes = _long_scene_ranges(beats, {"scenes": [{"start_beat_id": 0, "end_beat_id": 2},
                                                       {"start_beat_id": 3, "end_beat_id": 5},
                                                       {"start_beat_id": 29, "end_beat_id": 29}]})
        self.assertEqual([(s["start_beat_id"], s["end_beat_id"]) for s in scenes], [(0, 2), (3, 5), (29, 29)])
        self.assertEqual(sum(s["end"] - s["start"] for s in scenes), 140)

    def test_backward_expansion_dense_sampling_and_cached_decisions(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps({"source": str(root / "source.mp4"), "duration": 1000}))
            (root / "transcript.json").write_text('{"words":[]}')
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 900}
            beats = [{"id": 0, "start": 0, "end": 800}]
            scene = {"start_beat_id": 0, "end_beat_id": 0, "start": 50, "end": 100, "purpose": "fight", "required": True}
            answer = decision(event(26, 96, action=28, outcome=90, reaction=94, downtime=96))
            answer.update(cold_open_start=86, cold_open_end=96)
            ai = AI(root, mock=True)
            ai.mock = False  # Exercise actual anchor/window validation without API calls.

            def frames(_source, _output, interval, *, start, end):
                return [{"timestamp": start + i * interval, "path": "frame.jpg"}
                        for i in range(int((end - start) / interval))]

            with patch("milloclips.editing.sample_boundary_frames", side_effect=frames) as sampling, \
                    patch.object(ai, "refine_long_scene", side_effect=[{"needs_earlier_context": True}, answer,
                                                                     {"needs_later_context": True}, answer]) as calls:
                cuts, _hook = _refine_long_scenes(root, ai, game, beats, [scene], {"editorial_end": 900}, 0, False)
                self.assertEqual(cuts[0]["relative_start"], 26)
                self.assertEqual(calls.call_args_list[1].args[0]["allowed_region"][0], 0)
                self.assertIn(0.25, [call.args[2] for call in sampling.call_args_list])
                _refine_long_scenes(root, ai, game, beats, [scene], {"editorial_end": 900}, 0, False)
                self.assertEqual(calls.call_count, 4)

    def test_invalid_refinement_is_reported_instead_of_rough_fallback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text('{"source":"source.mp4","duration":100}')
            (root / "transcript.json").write_text('{"words":[]}')
            ai = AI(root, mock=True)
            with patch("milloclips.editing.sample_boundary_frames", return_value=[]), \
                    patch.object(ai, "refine_long_scene", return_value={"start": 10, "end": 30}):
                with self.assertRaisesRegex(ValueError, "needs review"):
                    _refine_long_scenes(root, ai, {"id": "game", "gameplay_start": 0}, [{"id": 0, "start": 0, "end": 100}],
                                        [{"start_beat_id": 0, "end_beat_id": 0, "start": 10, "end": 30}],
                                        {"editorial_end": 100}, 0, False)
            self.assertEqual(json.loads((root / "long-boundary-review.json").read_text())["status"], "needs_review")

    def test_budget_revision_preserves_approved_ranges_and_required_premise(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text('{"source":"source.mp4","duration":1200}')
            beats = [{"id": i, "start": i * 100, "end": (i + 1) * 100, "phase": "IN_GAME"} for i in range(12)]
            initial = {"scenes": [{"start_beat_id": 0, "end_beat_id": 3, "purpose": "premise"},
                                   {"start_beat_id": 4, "end_beat_id": 6, "purpose": "optional"},
                                   {"start_beat_id": 10, "end_beat_id": 11, "purpose": "ending"}], "cold_open_beat_id": 11}
            revised = {**initial, "scenes": [initial["scenes"][0], initial["scenes"][2]]}
            ai = AI(root, mock=True)

            def refined(_root, _ai, _game, _beats, scenes, _key, _cold, _force):
                return ([{"source_start": s["start"], "source_end": s["end"], "relative_start": s["start"],
                          "relative_end": s["end"]} for s in scenes], {"source_start": 1100, "source_end": 1110})

            with patch("milloclips.editing.selected_game", return_value={"id": "game", "gameplay_start": 0, "gameplay_end": 1200}), \
                    patch("milloclips.editing._selected_beats", return_value=beats), \
                    patch("milloclips.editing._refine_long_scenes", side_effect=refined), \
                    patch("milloclips.editing._apply_long_intro", side_effect=lambda _root, _ai, plan, *_args: {**plan, "intro_policy_version": 1}), \
                    patch.object(ai, "plan_long", side_effect=[initial, revised]):
                plan = plan_long(root, ai)
                self.assertEqual([(s["source_start"], s["source_end"]) for s in plan["segments"]], [(0, 400), (1000, 1200)])
                self.assertEqual(plan["duration"], 627.5)
                broken = copy.deepcopy(revised)
                broken["scenes"] = [broken["scenes"][-1]]
                with patch.object(ai, "plan_long", side_effect=[initial, broken]), self.assertRaisesRegex(ValueError, "required premise"):
                    plan_long(root, ai, force=True)

    def test_editorial_model_and_transcript_changes_invalidate_plan_cache(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text('{"source":"source.mp4","duration":1000}')
            (root / "transcript.json").write_text('{"words":[]}')
            beats = [{"id": 0, "start": 0, "end": 600, "phase": "IN_GAME"}]
            ai = AI(root, mock=True)
            proposal = {"scenes": [{"start_beat_id": 0, "end_beat_id": 0}], "cold_open_beat_id": 0}
            cuts = ([{"source_start": 0, "source_end": 600, "relative_start": 0, "relative_end": 600}],
                    {"source_start": 590, "source_end": 600})
            with patch("milloclips.editing.selected_game", return_value={"id": "game", "gameplay_start": 0, "gameplay_end": 600}), \
                    patch("milloclips.editing._selected_beats", return_value=beats), \
                    patch("milloclips.editing._refine_long_scenes", return_value=cuts), \
                    patch("milloclips.editing._apply_long_intro", side_effect=lambda _root, _ai, plan, *_args: {**plan, "intro_policy_version": 1}), \
                    patch.object(ai, "plan_long", return_value=proposal) as propose:
                first = plan_long(root, ai)
                self.assertEqual(plan_long(root, ai), first)
                self.assertEqual(propose.call_count, 1)
                ai.editorial_model = "different-model"
                plan_long(root, ai)
                self.assertEqual(propose.call_count, 2)
                (root / "transcript.json").write_text('{"words":[{"word":"corrected","start":0,"end":1}]}')
                plan_long(root, ai)
                self.assertEqual(propose.call_count, 3)
