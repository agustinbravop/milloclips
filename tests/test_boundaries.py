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
    def test_final_result_finishes_dialogue_then_retains_one_speech_free_second(self):
        part = event(end=30, downtime=30)
        words = [{"word": "perdimos", "start": 29.8, "end": 30.4},
                 {"word": "chat", "start": 30.7, "end": 31.2},
                 {"word": "siguiente", "start": 34, "end": 34.5}]
        frames = [{"timestamp": t} for t in range(51)]
        result = _long_boundary_segments(decision(part), (0, 50), words, frames, final_scene=True)
        self.assertAlmostEqual(result[-1]["end"], 32.2)
        with self.assertRaisesRegex(ValueError, "speech-free"):
            _long_boundary_segments(decision(part), (0, 32), words, frames, final_scene=True)

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

    def test_adjacent_events_share_pre_action_context_without_repeating_footage(self):
        premise = {**event(57, 64, action=59, outcome=62, reaction=63, downtime=None),
                   "event_kind": "speech", "source_start": 157, "source_end": 164, "scene_id": "premise"}
        fight = {**event(59, 132, action=70, outcome=129, reaction=130, downtime=None),
                 "source_start": 159, "source_end": 232, "relative_start": 59, "scene_id": "fight"}
        reaction = {**event(122, 129, action=123, outcome=127, reaction=128, downtime=None),
                    "event_kind": "speech", "source_start": 222, "source_end": 229, "scene_id": "reaction", "required": True}
        result = _deduplicate_long_events([premise, fight, reaction])
        self.assertEqual([(p["source_start"], p["source_end"]) for p in result], [(157, 164), (164, 232)])
        self.assertEqual((result[1]["start"], result[1]["relative_start"]), (64, 64))
        self.assertTrue(result[1]["required"])

    def test_duplicate_event_scouts_keep_union_of_approved_boundaries(self):
        first = {**event(), "source_start": 110, "source_end": 130, "scene_id": "first"}
        second = {**event(11, 31, action=13, outcome=27, reaction=29, downtime=31),
                  "source_start": 111, "source_end": 131, "scene_id": "second", "required": True}
        result = _deduplicate_long_events([first, second])
        self.assertEqual([(p["source_start"], p["source_end"]) for p in result], [(110, 131)])
        self.assertEqual(result[0]["end"], 31)
        self.assertTrue(result[0]["required"])

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

    def test_short_frame_indices_resolve_to_inspected_times_without_changing_raw_output(self):
        from milloclips.editing import _short_frame_decision

        frames = [{"timestamp": time} for time in (0, 2, 4, 6)]
        raw = {"needs_earlier_context": False, "needs_later_context": False, "segments": [{
            "start_frame": 0, "action_frame": 1, "outcome_frame": 2, "reaction_frame": 2, "end_frame": 3,
            "event_kind": "combat", "start_reason": "approach", "end_reason": "round finished",
            "action_detail": "the champions engage", "outcome_detail": "the round loss is visible",
            "reaction_detail": "the immediate round aftermath"}]}
        resolved = _short_frame_decision(raw, frames)
        self.assertEqual(resolved["segments"][0]["outcome_time"], 4)
        self.assertNotIn("outcome_time", raw["segments"][0])
        self.assertEqual(_long_boundary_segments(resolved, (0, 8), [], frames, complete_combat=True)[0]["end"], 6)
        raw["segments"][0]["outcome_frame"] = 99
        with self.assertRaisesRegex(ValueError, "actual supplied image"):
            _short_frame_decision(raw, frames)

    def test_short_closes_ongoing_english_reaction_without_starting_new_talk(self):
        from milloclips.editing import _short_frame_decision

        raw = {"needs_earlier_context": False, "needs_later_context": False, "segments": [{
            "start_frame": 0, "action_frame": 1, "outcome_frame": 2, "reaction_frame": 2, "end_frame": 3,
            "event_kind": "combat", "start_reason": "approach", "end_reason": "round finished",
            "action_detail": "the champions engage", "outcome_detail": "the round loss is visible",
            "reaction_detail": "the immediate round aftermath"}]}
        words = [{"word": text, "start": start, "end": end}
                 for text, start, end in (("Oh", 3.5, 3.8), ("my", 3.8, 4.1), ("God", 4.1, 4.3), ("next", 4.9, 5.3))]
        cut = _short_frame_decision(raw, [{"timestamp": time} for time in (0, 2, 4, 6)], words)
        self.assertAlmostEqual(cut["segments"][0]["end"], 4.4)
        self.assertEqual(cut["segments"][0]["evidence"]["reaction_end"]["quote"], "God")
        quiet = _short_frame_decision(raw, [{"timestamp": time} for time in (0, 2, 4.6, 6)], words)
        self.assertLess(quiet["segments"][0]["end"], 4.9)

    def test_short_budget_removes_pre_action_lead_without_truncating_combat(self):
        from milloclips.editing import _fit_short_event_budget

        parts = [event(0, 70, action=30, outcome=66, reaction=68, downtime=None)]
        fitted = _fit_short_event_budget(parts, 60, [], [{"timestamp": time} for time in range(71)], 40)
        self.assertEqual(fitted[0]["start"], 10)
        self.assertEqual(fitted[0]["end"], 70)
        self.assertEqual(fitted[0]["outcome_time"], 66)
        self.assertEqual(parts[0]["start"], 0)
        with self.assertRaises(ValueError):
            _fit_short_event_budget(parts, 60, [], [{"timestamp": time} for time in range(71)], 8)

    def test_short_uses_shared_event_completion_and_dense_boundary_passes(self):
        from milloclips.editing import _edit_selected_short

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            (root / "manifest.json").write_text(json.dumps({"source": str(source), "duration": 1000}))
            (root / "transcript.json").write_text('{"words":[]}')
            (root / "beats.json").write_text(json.dumps({"beats": [{"id": 0, "start": 0, "end": 800}]}))
            game = {"id": "game-001", "gameplay_start": 100, "gameplay_end": 900}
            candidate = {"title": "Selected fight", "reason": "The complete fight", "hook": "A shout", "payoff": "A reaction",
                         "segments": [{"relative_start": 58, "relative_end": 76, "source_start": 158, "source_end": 176}],
                         "hook_evidence": {"kind": "visual", "before_time": 60, "time": 61, "detail": "the first action happens"},
                         "payoff_evidence": {"kind": "visual", "before_time": 70, "time": 71, "detail": "the streamer reacts early"},
                         "_images": ["verified-hook.jpg", "transient-payoff.jpg"]}
            complete = decision(event(46, 64, action=48, outcome=60, reaction=62, downtime=None),
                                event(68, 100, action=70, outcome=94, reaction=98, downtime=100))
            ai = AI(root, mock=True)
            ai.mock = False

            def frames(_source, _output, interval, *, start, end):
                return [{"timestamp": start + index * interval, "path": "frame.jpg"}
                        for index in range(int((end - start) / interval))]

            with patch("milloclips.editing._bounded_frames", side_effect=lambda frames, *_: frames), \
                    patch("milloclips.editing.sample_boundary_frames", side_effect=frames) as sampling, \
                    patch.object(ai, "refine_long_scene", side_effect=[{"needs_later_context": True}, complete, complete,
                                                                     {"needs_later_context": True}, complete, complete]) as requests:
                cut = _edit_selected_short(root, ai, candidate, game, source, "tag", [], {}, {"editorial_end": 900}, [], [])
                self.assertEqual(cut["relative_start"], 46)
                self.assertEqual(cut["relative_end"], 100)
                self.assertEqual(cut["segments"][0]["outcome_time"], 94)
                self.assertEqual(cut["duration"], 54)
                self.assertEqual(len(cut["segments"]), 1)
                self.assertEqual(requests.call_args.args[0]["format"], "short")
                self.assertIn({"timestamp": 71, "path": "transient-payoff.jpg"}, requests.call_args_list[0].args[3])
                self.assertIn(0.25, [call.args[2] for call in sampling.call_args_list])
                self.assertEqual(_edit_selected_short(root, ai, candidate, game, source, "tag", [], {}, {"editorial_end": 900}, [], []), cut)
                self.assertEqual(requests.call_count, 3)
                def more_frames(*args, **kwargs):
                    sampled = frames(*args, **kwargs)
                    extra = {"timestamp": kwargs["start"] + args[2] / 2, "path": "extra.jpg"}
                    return sorted([*sampled, extra], key=lambda frame: frame["timestamp"])
                sampling.side_effect = more_frames
                self.assertEqual(_edit_selected_short(root, ai, candidate, game, source, "tag", [], {}, {"editorial_end": 900}, [], []), cut)
                self.assertEqual(requests.call_count, 6)

    def test_backward_expansion_reuses_coarse_decisions_without_dense_rereview(self):
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

            with patch("milloclips.editing._bounded_frames", side_effect=lambda frames, *_: frames), \
                    patch("milloclips.editing.sample_boundary_frames", side_effect=frames) as sampling, \
                    patch.object(ai, "refine_long_scene", side_effect=[{"needs_earlier_context": True}, answer,
                                                                     {"needs_later_context": True}, answer]) as calls:
                cuts, _hook = _refine_long_scenes(root, ai, game, beats, [scene], {"editorial_end": 900}, 0, False)
                self.assertEqual(cuts[0]["relative_start"], 26)
                self.assertEqual(calls.call_args_list[1].args[0]["allowed_region"][0], 0)
                self.assertNotIn(0.25, [call.args[2] for call in sampling.call_args_list])
                _refine_long_scenes(root, ai, game, beats, [scene], {"editorial_end": 900}, 0, False)
                self.assertEqual(calls.call_count, 2)

    def test_final_scene_can_inspect_cached_result_context_beyond_scanner_gameplay_end(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text('{"source":"source.mp4","duration":120}')
            (root / "transcript.json").write_text('{"words":[]}')
            ai = AI(root, mock=True)
            game = {"id": "game", "gameplay_start": 0, "gameplay_end": 100, "postgame_end": 110}
            scene = {"start_beat_id": 0, "end_beat_id": 0, "start": 80, "end": 100, "final_scene": True, "purpose": "ending"}
            answer = decision(event(80, 104, action=82, outcome=102, reaction=103, downtime=None))
            with patch("milloclips.editing.sample_boundary_frames", return_value=[]), \
                    patch("milloclips.editing.extract_audio"), \
                    patch.object(ai, "transcribe", return_value={"words": [{"word": "fin", "start": 2, "end": 3}]}) as transcribe, \
                    patch.object(ai, "refine_long_scene", return_value=answer) as refine:
                cuts, _ = _refine_long_scenes(root, ai, game, [{"id": 0, "start": 0, "end": 100}],
                                             [scene], {"editorial_end": 100}, None, False, require_intro_hint=False)
                self.assertEqual(cuts[-1]["source_end"], 104)
                self.assertEqual(refine.call_args.args[0]["allowed_region"][1], 110)
                self.assertIn({"word": "fin", "start": 102, "end": 103}, refine.call_args.args[2])
                _refine_long_scenes(root, ai, game, [{"id": 0, "start": 0, "end": 100}],
                                    [scene], {"editorial_end": 100}, None, False, require_intro_hint=False)
                self.assertEqual(transcribe.call_count, 1)

    def test_invalid_review_retains_best_available_source_window_with_a_note(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text('{"source":"source.mp4","duration":100}')
            (root / "transcript.json").write_text('{"words":[]}')
            ai = AI(root, mock=True)
            with patch("milloclips.editing.sample_boundary_frames", return_value=[]), \
                    patch.object(ai, "refine_long_scene", return_value={"start": 10, "end": 30}):
                cuts, _ = _refine_long_scenes(root, ai, {"id": "game", "gameplay_start": 0}, [{"id": 0, "start": 0, "end": 100}],
                                            [{"start_beat_id": 0, "end_beat_id": 0, "start": 10, "end": 30}],
                                            {"editorial_end": 100}, 0, False)
            self.assertEqual((cuts[0]["source_start"], cuts[0]["source_end"]), (8, 33))
            self.assertTrue(cuts[0]["boundary_note"])
            self.assertTrue(json.loads((root / "long-boundary-review.json").read_text())["notes"])

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
                self.assertEqual([(s["source_start"], s["source_end"]) for s in plan["segments"]], [(25, 400), (1000, 1200)])
                self.assertEqual(plan["intro"]["source_start"], 0)
                self.assertEqual(plan["intro"]["source_end"], 25)
                self.assertEqual(plan["duration"], 617.5)
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
