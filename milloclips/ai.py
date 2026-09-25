from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from .io import write_json


STATES = ["OTHER", "QUEUE", "CHAMP_SELECT", "LOADING", "IN_GAME", "POST_GAME"]
BEAT_TYPES = ["SETUP", "COMBAT", "REACTION", "STRATEGY", "FARMING", "TRAVEL", "SHOPPING", "DEAD", "RESULT"]


class AI:
    def __init__(self, vod_dir: Path, model: str = "gpt-5-nano", mock: bool = False):
        self.vod_dir = vod_dir
        self.model = model
        self.mock = mock
        self.client = None if mock else OpenAI(api_key=self._key())

    @staticmethod
    def _key() -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY is required for real analysis; use --mock for a local fixture run")
        return key

    def _json(self, prompt: str, images: list[Path] | None = None) -> Any:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images or []:
            encoded = base64.b64encode(image.read_bytes()).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"},
                }
            )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        usage = response.usage
        with (self.vod_dir / "usage.jsonl").open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "model": self.model,
                        "input_tokens": usage.prompt_tokens if usage else None,
                        "output_tokens": usage.completion_tokens if usage else None,
                    }
                )
                + "\n"
            )
        return json.loads(response.choices[0].message.content or "{}")

    def classify_frames(self, frames: list[dict], total_duration: float) -> list[dict]:
        if self.mock:
            output = []
            for frame in frames:
                fraction = frame["timestamp"] / max(total_duration, 1)
                state = (
                    "OTHER" if fraction < 0.10
                    else "CHAMP_SELECT" if fraction < 0.18
                    else "LOADING" if fraction < 0.23
                    else "IN_GAME" if fraction < 0.80
                    else "POST_GAME" if fraction < 0.90
                    else "OTHER"
                )
                output.append({"timestamp": frame["timestamp"], "state": state, "confidence": 0.99, "frame": frame["path"]})
            return output

        results: list[dict] = []
        for offset in range(0, len(frames), 12):
            batch = frames[offset : offset + 12]
            labels = [f"image {index}: {item['timestamp']:.1f}s" for index, item in enumerate(batch)]
            prompt = f"""
Classify each timestamped streaming screenshot as exactly one of {STATES}.
Return JSON only: {{"frames":[{{"image":0,"state":"IN_GAME","confidence":0.9}}]}}.
Use IN_GAME only for visible League gameplay HUD, not loading or champion select.
Images appear in this order:\n""" + "\n".join(labels)
            parsed = self._json(prompt, [Path(item["path"]) for item in batch])
            by_index = {item["image"]: item for item in parsed.get("frames", []) if isinstance(item.get("image"), int)}
            for index, frame in enumerate(batch):
                answer = by_index.get(index, {})
                state = answer.get("state", "OTHER")
                if state not in STATES:
                    state = "OTHER"
                results.append(
                    {
                        "timestamp": frame["timestamp"],
                        "state": state,
                        "confidence": float(answer.get("confidence", 0)),
                        "frame": frame["path"],
                    }
                )
        return results

    def transcribe(self, audio: Path, duration_seconds: float | None = None) -> dict:
        if self.mock:
            words = []
            vocabulary = ["voy", "a", "gankear", "ahora", "no", "puede", "ser", "vamos"]
            for index in range(max(1, int((duration_seconds or 300) / 2))):
                words.append({"word": vocabulary[index % len(vocabulary)], "start": index * 2.0, "end": index * 2.0 + 0.8})
            return {"text": " ".join(word["word"] for word in words), "words": words, "language": "es"}
        with audio.open("rb") as file:
            glossary = json.loads(Path(__file__).with_name("league_glossary.json").read_text(encoding="utf-8"))
            vocabulary_file = self.vod_dir / "vocabulary.json"
            custom = json.loads(vocabulary_file.read_text(encoding="utf-8")).get("terms", []) if vocabulary_file.exists() else []
            if not isinstance(custom, list):
                raise ValueError("vocabulary.json terms must be a list")
            vocabulary = list(
                dict.fromkeys(
                    [
                        *(str(term).strip() for term in custom if str(term).strip()),
                        *glossary["priority_terms"],
                    ]
                )
            )
            result = self.client.audio.transcriptions.create(
                file=file,
                model="whisper-1",
                language="es",
                response_format="verbose_json",
                timestamp_granularities=["word"],
                prompt=", ".join(vocabulary)[:800],
            )
        return result.model_dump()

    def classify_beats(self, beats: list[dict], image_paths: list[Path]) -> list[dict]:
        if self.mock:
            kinds = ["SETUP", "FARMING", "COMBAT", "REACTION", "TRAVEL", "STRATEGY"]
            return [
                {
                    **beat,
                    "type": kinds[beat["id"] % len(kinds)],
                    "importance": 5 if beat["id"] % 7 == 0 else 3 if beat["id"] % 3 == 0 else 1,
                    "keep": beat["id"] % 4 != 1,
                    "reason": "synthetic fixture classification",
                }
                for beat in beats
            ]

        prompt = f"""
Classify each League gameplay beat. Allowed types: {BEAT_TYPES}.
Importance is 1 (routine) to 5 (essential). Keep only beats useful to a coherent
game story. Walking/farming is removable unless dialogue or setup matters.
CHAMP_SELECT is useful only when speech or chat establishes a premise; loading
visuals alone are not useful. A scoreboard opened during gameplay is not the
match result. Use RESULT only for a clear local payoff or the actual game ending.
Return JSON only: {{"beats":[{{"id":0,"type":"COMBAT","importance":4,"keep":true,"reason":"brief reason"}}]}}.
Beats:\n{json.dumps([{k: v for k, v in beat.items() if k != 'frames'} for beat in beats], ensure_ascii=False)}
Images are grouped in beat order, three images per beat.
"""
        parsed = self._json(prompt, image_paths)
        answers = {item["id"]: item for item in parsed.get("beats", []) if isinstance(item.get("id"), int)}
        output = []
        for beat in beats:
            answer = answers.get(beat["id"], {})
            kind = answer.get("type", "FARMING")
            output.append(
                {
                    **beat,
                    "type": kind if kind in BEAT_TYPES else "FARMING",
                    "importance": max(1, min(5, int(answer.get("importance", 1)))),
                    "keep": bool(answer.get("keep", False)),
                    "reason": str(answer.get("reason", "no reason supplied"))[:160],
                }
            )
        return output

    def plan_long(self, beats: list[dict]) -> dict:
        if self.mock:
            return {"selected_ids": [beat["id"] for beat in beats if beat["keep"]], "story": "Synthetic game rough cut."}
        compact = [
            {key: beat[key] for key in ("id", "start", "end", "type", "importance", "text", "reason")}
            for beat in beats
        ]
        parsed = self._json(
            "Select chronological variable-length beats totaling 9-12 minutes for a coherent League video. "
            "Open on meaningful speech or immediately understandable action, not obligatory first footage. "
            "Build a complete arc across early, middle, and late game: premise, setup, escalation, major turns, "
            "payoff, and the actual Nexus/result ending. Select complete adjacent scene runs with setup, action, "
            "and reaction rather than isolated beats that begin or end mid-thought. Do not "
            "select more than six consecutive beats. Exclude routine loading, travel, farming, shopping, and dead "
            "time unless dialogue or setup makes it necessary. Return JSON only: "
            '{"selected_ids":[0,1],"story":"one sentence"}. Beats:\n'
            + json.dumps(compact, ensure_ascii=False)
        )
        return {"selected_ids": parsed.get("selected_ids", []), "story": str(parsed.get("story", ""))}

    def shorts(self, beats: list[dict]) -> list[dict]:
        evidence = [
            {
                **{key: beat.get(key) for key in ("id", "start", "end", "type", "importance", "keep", "text", "reason")},
                "anchor": beat["importance"] >= 3 or beat["type"] in {"COMBAT", "REACTION"},
            }
            for beat in beats
        ]
        if not evidence:
            return []
        if self.mock:
            anchors = [beat for beat in evidence if beat["anchor"]] or evidence[:1]
            return [
                {
                    "start_beat_id": beat["id"],
                    "end_beat_id": min(beat["id"] + 1, beats[-1]["id"]),
                    "title": f"Moment {beat['id']}",
                    "reason": beat["reason"],
                    "scores": {"hook": beat["importance"], "context": 3, "payoff": beat["importance"], "tightness": 3},
                }
                for beat in anchors[:10]
            ]
        parsed = self._json(
            "Find up to 10 self-contained Spanish League short candidates. The first two seconds must contain "
            "meaningful speech or immediately understandable action. Include enough setup to understand intent, "
            "then a clear payoff and at most a brief reaction. End before recall, travel, farming, repeated "
            "commentary, or other dead tail. Reject moments that need missing context. Select one contiguous beat "
            "range lasting 8-90 seconds per candidate. Anchor=true marks promising moments, but surrounding "
            "anchor=false beats remain available when needed for setup or payoff. Score hook, context, payoff, and "
            "tightness separately from 1 (poor) to 5 (excellent). Return JSON only: "
            "{\"candidates\":[{\"start_beat_id\":0,\"end_beat_id\":2,\"title\":\"...\",\"reason\":\"...\","
            "\"scores\":{\"hook\":5,\"context\":4,\"payoff\":5,\"tightness\":4}}]}. Beat evidence:\n"
            + json.dumps(evidence, ensure_ascii=False)
        )
        return parsed.get("candidates", [])

    def detect_layout(self, frame: Path) -> dict:
        if self.mock:
            return {"facecam": [0.02, 0.03, 0.28, 0.30], "confidence": 0.9}
        parsed = self._json(
            "Locate the streamer's real facecam, excluding champion portraits. Return normalized "
            'coordinates as JSON only: {"facecam":[x,y,width,height],"confidence":0.0}.',
            [frame],
        )
        box = parsed.get("facecam", [0.02, 0.03, 0.28, 0.30])
        if len(box) != 4 or any(not isinstance(value, (int, float)) for value in box):
            box = [0.02, 0.03, 0.28, 0.30]
        return {"facecam": [max(0, min(1, float(value))) for value in box], "confidence": parsed.get("confidence", 0)}
