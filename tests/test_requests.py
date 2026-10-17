import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import RateLimitError

from milloclips.ai import AI, MAX_REQUEST_IMAGES
from milloclips.editing import _bounded_frames, _validate_short_candidates


class RequestTests(unittest.TestCase):
    def test_unchanged_request_reuses_raw_response_without_spending(self):
        with tempfile.TemporaryDirectory() as folder:
            ai = AI(Path(folder), mock=True)
            ai.mock = False
            ai.client = Mock()
            ai.client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3))
            self.assertEqual(ai._json("same input"), {"ok": True})
            self.assertEqual(ai._json("same input"), {"ok": True})
            ai.client.chat.completions.create.assert_called_once()
            self.assertTrue(json.loads((Path(folder) / "usage.jsonl").read_text())["timestamp"])

    def test_exhausted_credit_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            ai = AI(Path(folder), mock=True)
            ai.client = Mock()
            ai.client.chat.completions.create.side_effect = RateLimitError(
                "quota", response=SimpleNamespace(status_code=429, request=Mock(), headers={}),
                body={"code": "insufficient_quota"})
            with self.assertRaises(RateLimitError), patch("milloclips.ai.time.sleep") as sleep:
                ai._json("first")
            sleep.assert_not_called()
            ai.client.chat.completions.create.assert_called_once()

    def test_frame_cap_preserves_supplied_event_anchors(self):
        frames = [{"timestamp": i, "path": str(i)} for i in range(400)]
        selected = _bounded_frames(frames, [7, 19, 178, 395])
        self.assertLessEqual(len(selected), MAX_REQUEST_IMAGES)
        self.assertTrue({7, 19, 178, 395} <= {f["timestamp"] for f in selected})

    def test_starting_payoff_anchor_does_not_prove_its_sentence_was_retained(self):
        clipped = {"source_start": 100, "source_end": 110, "relative_start": 0, "relative_end": 10,
                   "duration": 10, "payoff_evidence": {"kind": "speech", "time": 8}, "payoff_end": 12}
        with self.assertRaisesRegex(ValueError, "complete payoff"):
            _validate_short_candidates([clipped], 100, 160, 200)
        complete = {**clipped, "source_end": 115, "relative_end": 15, "duration": 15}
        self.assertEqual(_validate_short_candidates([complete], 100, 160, 200), [complete])
