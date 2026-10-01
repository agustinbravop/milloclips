import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from milloclips.ai import AI
from milloclips.editing import _apply_long_intro, _extend_intro_body, _long_transitions, _validate_long_intro
from milloclips.media import intro_duration


class IntroTests(unittest.TestCase):
    def test_short_setup_extends_only_new_main_events_without_revisiting_approved_cuts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            beats = [{"id": i, "start": i * 10, "end": (i + 1) * 10, "phase": "IN_GAME"} for i in range(60)]
            original = {"cold_open_beat_id": 59, "scenes": [
                {"start_beat_id": 0, "end_beat_id": 9}, {"start_beat_id": 10, "end_beat_id": 39},
                {"start_beat_id": 50, "end_beat_id": 59}]}
            revised = {**original, "scenes": [*original["scenes"], {"start_beat_id": 40, "end_beat_id": 49}]}
            (root / "long-proposals.json").write_text(json.dumps({"plan": original}))
            (root / "long-cuts.json").write_text('{"game_id":"game","editorial_end":700,"cuts_version":2,"answers":{}}')
            def cut(start, end, kind, key):
                return {"source_start": start + 100, "source_end": end + 100, "relative_start": start,
                        "event_kind": kind, "scene_id": key, "action_start": start + 2}
            approved = [cut(0, 100, "speech", "0:9"), cut(100, 400, "combat", "10:39"), cut(500, 600, "combat", "50:59")]
            extra = cut(400, 500, "combat", "40:49")
            ai = AI(root, mock=True)
            plan = {"title_duration": 5, "end_card_duration": 12}
            intro = {"kind": "setup", "source_start": 100, "source_end": 110}
            with patch.object(ai, "plan_long", return_value=revised) as propose, \
                    patch("milloclips.editing.selected_game", return_value={"id": "game", "gameplay_start": 100}), \
                    patch("milloclips.editing._refine_long_scenes", return_value=([extra], None)) as refine:
                result = _extend_intro_body(root, ai, plan, beats, intro, approved, 535)
                self.assertEqual(result, [approved[0], approved[1], extra, approved[2]])
                self.assertEqual([(s["start_beat_id"], s["end_beat_id"]) for s in refine.call_args.args[4]], [(40, 49)])
                self.assertFalse(refine.call_args.kwargs["require_intro_hint"])
                self.assertEqual(propose.call_args.kwargs["budget_feedback"]["approved_segments"], approved[1:])

    def test_failed_or_unproved_play_cannot_be_a_cold_open(self):
        events = [{"id": 0, "start": 0, "end": 20, "setup_eligible": False}]
        answer = {"kind": "cold_open", "subject": "elmiillor", "outcome": "failure", "cool_play": True,
                  "complete": True, "related_to_selected_game": True, "segments": [{"event_id": 0, "start": 0, "end": 10}],
                  "evidence": {"kind": "visual", "time": 9, "detail": "ElmilloR visibly wins the engagement"}}
        limits = {"cold_open": [5, 20], "setup": [8, 40]}
        with self.assertRaisesRegex(ValueError, "Failed plays"):
            _validate_long_intro(answer, events, limits, [], [{"timestamp": 9}], 100)
        answer["outcome"] = "success"
        with self.assertRaisesRegex(ValueError, "inspected visual"):
            _validate_long_intro(answer, events, limits, [], [], 100)
        answer["subject"] = "teammate"
        with self.assertRaisesRegex(ValueError, "own successful play"):
            _validate_long_intro(answer, events, limits, [], [{"timestamp": 9}], 100)
        answer["subject"] = "elmiillor"
        self.assertEqual(_validate_long_intro(answer, events, limits, [], [{"timestamp": 9}], 100)["kind"], "cold_open")

    def test_setup_retry_skips_loading_and_preserves_cached_raw_outputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.touch()
            (root / "manifest.json").write_text(json.dumps({"source": str(source)}))
            (root / "transcript.json").write_text('{"words":[]}')
            (root / "pregame-transcript.json").write_text(json.dumps({"audio_start": 90, "words": [
                {"word": "Voy", "start": 1, "end": 1.5}, {"word": "Udyr", "start": 1.5, "end": 2}]}))
            plan = {"gameplay_start": 100, "story": "AP Udyr", "title_duration": 5, "end_card_duration": 12,
                    "cold_open": {"source_start": 450, "source_end": 460}, "segments": [
                        {"source_start": 90, "source_end": 100, "relative_start": -10, "event_kind": "speech"},
                        {"source_start": 105, "source_end": 115, "relative_start": 5, "event_kind": "speech"},
                        {"source_start": 120, "source_end": 660, "relative_start": 20, "event_kind": "combat"}]}
            beats = [{"id": -1, "start": -10, "end": 0, "text": "Voy Udyr", "phase": "CHAMP_SELECT"},
                     {"id": 0, "start": 5, "end": 15, "text": "AP build", "phase": "IN_GAME"},
                     {"id": 1, "start": 20, "end": 560, "text": "fight", "phase": "IN_GAME"}]
            setup = {"kind": "setup", "subject": "elmiillor", "outcome": "setup", "complete": True,
                     "related_to_selected_game": True, "reason": "Champion and build premise", "segments": [
                         {"event_id": 0, "start": -10, "end": 0}, {"event_id": 1, "start": 5, "end": 15}],
                     "evidence": {"kind": "speech", "time": -9, "quote": "Voy Udyr"}}
            fail = {**setup, "kind": "cold_open", "outcome": "failure"}
            ai = AI(root, mock=True)
            ai.mock = False
            def sampled(_source, _path, _interval, *, start, end):
                return [{"timestamp": start, "path": "frame.jpg"}, {"timestamp": end - 0.1, "path": "frame.jpg"}]

            with patch("milloclips.editing.sample_boundary_frames", side_effect=sampled), \
                    patch.object(ai, "propose_long_intro", side_effect=[{"kind": "cold_open", "event_ids": [2]},
                                                                      {"kind": "setup", "event_ids": [0, 1]}]) as propose, \
                    patch.object(ai, "refine_long_intro", side_effect=[fail, setup]) as refine:
                updated = _apply_long_intro(root, ai, copy.deepcopy(plan), beats)
                self.assertEqual(updated["segments"], [plan["segments"][-1]])
                self.assertEqual(updated["story_segments"], plan["segments"])
                self.assertEqual(intro_duration(updated["intro"]), 20)
                self.assertEqual(updated["duration"], 575.5)
                times = [join["output_time"] for join in _long_transitions(updated)]
                self.assertEqual(times, [10, 19.5, 24.0, 563.5])
                again = _apply_long_intro(root, ai, updated, beats)
                self.assertEqual(again, updated)
                self.assertEqual(propose.call_count, 2)
                self.assertEqual(refine.call_count, 2)
                raw = json.loads((root / "long-intro.json").read_text())
                self.assertEqual(raw["attempts"][0]["refinement"]["outcome"], "failure")
                self.assertIn("validation_error", raw["attempts"][0])
