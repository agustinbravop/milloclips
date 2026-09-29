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
    def __init__(self, vod_dir: Path, model: str = "gpt-5-nano", mock: bool = False, shorts_model: str = "gpt-5-mini"):
        self.vod_dir = vod_dir
        self.model = model
        self.shorts_model = shorts_model
        self.mock = mock
        self.client = None if mock else OpenAI(api_key=self._key())

    @staticmethod
    def _key() -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY is required for real analysis; use --mock for a local fixture run")
        return key

    def _json(self, prompt: str, images: list[Path] | None = None, *, model: str | None = None) -> Any:
        model = model or self.model
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
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        usage = response.usage
        with (self.vod_dir / "usage.jsonl").open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "model": model,
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
                    "scenes": [{"start_beat_id": beat["id"], "end_beat_id": min(beat["id"] + 1, beats[-1]["id"])}],
                    "payoff_beat_id": min(beat["id"] + 1, beats[-1]["id"]),
                    "title": f"Moment {beat['id']}",
                    "proposed_payoff": beat["reason"],
                }
                for beat in anchors[:12]
            ]
        candidates = []
        for offset in range(0, len(evidence), 20):
            window = evidence[max(0, offset - 4) : offset + 24]
            parsed = self._json(
                "Return JSON only. Find up to THREE DIFFERENT events in this Spanish League window that could make a "
                "funny, amazing, or surprising TikTok even to someone who did not watch the stream. "
                "Start with the ACTUAL payoff: a spectacular play, disastrous blunder, unexpected reversal, "
                "or a funny contradiction/confession. Quote its exact speech if present. "
                "An ordinary recall, tactical comment, cooldown, item, objective plan, retreat, or mention of "
                "a kill is NOT an event without an exceptional visible or spoken outcome. Return zero if none. "
                "Beat labels and importance may be wrong. Give the ID of the beat containing the payoff. "
                "Use one continuous scene for a play, or 3-7 chronological scenes only for a real callback. "
                "Each scene uses inclusive beat IDs; trim to 8-90 seconds later. "
                "Return {\"candidates\":[{\"title\":\"...\",\"proposed_payoff\":\"specific event\","
                "\"payoff_beat_id\":2,\"scenes\":[{\"start_beat_id\":1,\"end_beat_id\":2}]}]}. Beats:\n"
                + json.dumps(window, ensure_ascii=False), model=self.shorts_model,
            )
            if isinstance(parsed.get("candidates"), list):
                candidates.extend(parsed["candidates"][:3])
        # A separate wide view may find a callback that spans several minutes.
        parsed = self._json(
            "Return JSON only. Find at most TWO surprising or hilarious callbacks across this League game. "
            "An actual ending must overturn or deliver on an earlier premise; no generic game recaps, "
            "plans, or unrelated highlights. Zero is fine. Use 3-7 short chronological scenes, "
            "8-90 seconds total after trimming, and identify the exact payoff beat. "
            "Return {\"candidates\":[{\"title\":\"...\",\"proposed_payoff\":\"...\","
            "\"payoff_beat_id\":2,\"scenes\":[{\"start_beat_id\":1,\"end_beat_id\":2}]}]}. Beats:\n"
            + json.dumps(evidence, ensure_ascii=False), model=self.shorts_model,
        )
        if isinstance(parsed.get("candidates"), list):
            candidates.extend(parsed["candidates"][:2])
        return candidates

    def visual_shorts(self, frames: list[dict]) -> list[dict]:
        if self.mock:
            return []
        parsed = self._json(
            "Return JSON only. The source transcript in this part of a League game is corrupted. "
            "These still images are in time order, not continuous video. Find at most ONE genuinely "
            "exceptional VISUAL Short where the streamer's champion visibly makes a decisive play, or "
            "an unmistakably surprising outcome occurs on screen. Use before/after frames as evidence. "
            "Do not mistake allies' plays, a routine death, position changes, an ordinary retreat, "
            "or a post-game screen for the streamer's highlight. No invented speech or unseen actions. "
            "Return {\"events\":[{\"start\":1900,\"end\":1930,\"payoff\":1925,"
            "\"description\":\"visible factual event\"}]} or {\"events\":[]}. "
            "Game-relative seconds per image: " + json.dumps([frame["timestamp"] for frame in frames]),
            [Path(frame["path"]) for frame in frames], model=self.shorts_model,
        )
        return parsed.get("events", []) if isinstance(parsed.get("events"), list) else []

    def callback_shorts(self, beats: list[dict], frames: list[dict]) -> list[dict]:
        if self.mock:
            return []
        parsed = self._json(
            "Return JSON only. The late-game transcript is corrupted, but these ordered gameplay frames "
            "show what actually happened. Look for at most ONE funny, verifiable callback: an earlier "
            "confident statement by the STREAMER about a specific teammate/champion or his own role, "
            "followed by a LATER VISIBLE outcome involving that same subject that proves him wrong. "
            "For example, a teammate he repeatedly mocked survives and helps win the fight while he is "
            "dead. Credit kills only if the images prove who got them; a team ace is not an individual "
            "multikill. Avoid unrelated highlights or fabricated reactions. If no specific three-act "
            "story with a beginning and ending is evidenced, return no candidates. If one exists, "
            "pick 3-7 BRIEF chronological beat ranges: spoken premise, turn/setback, visual payoff. "
            "The total selected footage should fit 8-90 seconds after trimming. Give payoff_beat_id "
            "as the REAL beat ID containing the visible outcome in the final scene, NEVER an example ID. "
            "Return a JSON object with candidates array, each having title, proposed_payoff, "
            "payoff_beat_id, and scenes array of start_beat_id/end_beat_id pairs. "
            "Gameplay frames at game-relative seconds: " + json.dumps([frame["timestamp"] for frame in frames])
            + ". Earlier beat evidence and late beat IDs: " + json.dumps(beats, ensure_ascii=False),
            [Path(frame["path"]) for frame in frames], model=self.shorts_model,
        )
        return parsed.get("candidates", []) if isinstance(parsed.get("candidates"), list) else []

    def locate_ace(self, frames: list[dict]) -> float | None:
        if self.mock:
            return None
        parsed = self._json(
            "Return JSON only. Which of these actual League frames visibly displays the large word "
            "ACE on screen? Do not infer it from dead champion portraits or a later push. ONLY cite "
            "an image where the literal ACE banner is present. Return {\"ace_time\":null} if none, "
            "otherwise {\"ace_time\":<exact supplied timestamp>}. Images are in order at "
            "game-relative seconds: " + json.dumps([frame["timestamp"] for frame in frames]),
            [Path(frame["path"]) for frame in frames], model=self.shorts_model,
        )
        time = parsed.get("ace_time")
        return float(time) if type(time) in (int, float) else None

    def verify_short(self, proposal: dict, beats: list[dict], frames: list[dict]) -> dict:
        if self.mock:
            return {
                "title": proposal.get("title", "Moment"), "story": "A complete synthetic moment",
                "hook": "The situation starts", "payoff": "The situation resolves",
                "hook_evidence": {"kind": "visual", "before_time": proposal["scenes"][0]["start"], "time": proposal["scenes"][0]["start"] + 1, "detail": "synthetic fixture opening"},
                "payoff_evidence": {"kind": "visual", "before_time": proposal["scenes"][-1]["end"] - 3, "time": proposal["scenes"][-1]["end"] - 2, "detail": "synthetic fixture ending"},
                "segments": [{"start": scene["start"], "end": scene["end"]} for scene in proposal["scenes"]],
            }
        return self._json(
            "Return JSON only. You are checking a Short for a stranger who will swipe in two seconds. "
            "Images are timestamped stills, NOT continuous video. Speech is timestamped separately. "
            "Reject ordinary gameplay commentary, recalls, plans without a surprising result, or a moment "
            "that needs a title to become interesting. Do NOT invent wins, kills or abilities from dialogue. "
            "If the hook cannot be HEARD or SEEN within the first 2 seconds of the actual cut, reject. "
            "If the payoff is not evidenced near the end of the cut, reject. A spoken self-correction can "
            "be a payoff. Return {\"reject\":true} if either fails. No scores. Otherwise return JSON "
            "fields title, story, hook, payoff, segments (start and end in seconds), hook_evidence and "
            "payoff_evidence. Prefer an EXACT SPOKEN phrase when available. Each evidence object has "
            "kind 'speech' or 'visual', time (seconds relative to "
            "GAME start), plus quote (EXACT words in transcript) for speech or detail (specific thing "
            "VISIBLE in supplied images) for visual. For visual evidence include before_time (an earlier "
            "supplied image showing the state before the action) and describe the actual CHANGE, not "
            "a static HUD, map or ordinary webcam. Both anchors must have different concrete content. "
            "Only use visual evidence at actual supplied image timestamps. A transient ACE banner can "
            "disappear in less than a second: cite the image where it IS visible, not a later frame. "
            "If a teammate was insulted earlier, do not claim that teammate personally got a multikill "
            "unless the kill feed explicitly credits them; a team ACE with that teammate alive is enough. "
            "Times are relative to GAME START, NOT the beginning of a scene; every segment must be inside "
            "its proposed scene range. Start with real action/speech; cut immediately after the payoff. "
            "Keep 1 scene for a self-contained play, or 2-7 for an arc; never pad to reach a scene count. "
            f"Proposal: {json.dumps(proposal, ensure_ascii=False)}. "
            f"Beats: {json.dumps(beats, ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames],
            model=self.shorts_model,
        )

    def choose_shorts(self, candidates: list[dict], images: list[Path]) -> list[int]:
        if self.mock:
            return list(range(min(2, len(candidates))))
        parsed = self._json(
            "Return JSON only. Choose at most TWO finished Shorts for YouTube Shorts/TikTok/Reels. "
            "Compare candidates head-to-head. Would a stranger stop scrolling in the FIRST TWO SECONDS, "
            "and is the payoff actually funny, amazing, or surprising? Prefer a remarkable moment over "
            "ordinary but coherent commentary. The supplied images are hook then payoff for each candidate "
            "in order, two images per candidate. Do not reward titles or claims unsupported by evidence. "
            "Choose ZERO if none are genuinely entertaining; do not fill a quota. "
            "Return {\"selected_ids\":[0,1],\"reason\":\"why these beat the alternatives\"}. "
            "Candidate evidence (zero-based IDs): " + json.dumps(candidates, ensure_ascii=False),
            images,
            model=self.shorts_model,
        )
        return parsed.get("selected_ids", []) if isinstance(parsed.get("selected_ids"), list) else []

    def refine_short(self, candidate: dict, words: list[dict], frames: list[dict]) -> dict:
        if self.mock:
            return {
                "title": candidate["title"], "story": candidate["reason"],
                "hook": candidate["hook"], "payoff": candidate["payoff"],
                "hook_evidence": candidate["hook_evidence"], "payoff_evidence": candidate["payoff_evidence"],
            }
        reversal = self._json(
            "Return JSON only. Find a FUNNY CONTRADICTION with THREE ACTS from the SAME speaker: "
            "1 a confident prediction that X is impossible, 2 X happens, 3 the speaker explicitly "
            "self-critiques his OWN reasoning. Choose the MOST absolute impossible-prediction as hook. "
            "The punchline MUST be the final FIRST-PERSON admission that his own understanding was "
            "mistaken, NOT the first sign of X, not a death or win, and not a justification of why an "
            "opponent did X. If no first-person admission, return {\"reject\":true}. "
            "Quote exact short consecutive speech for ALL three acts as "
            "{\"prediction_quote\":\"...\",\"event_quote\":\"...\",\"correction_quote\":\"...\"}. "
            "Words: " + json.dumps(words, ensure_ascii=False), model=self.shorts_model,
        )
        if all(isinstance(reversal.get(key), str) and reversal[key].strip() for key in ("prediction_quote", "event_quote", "correction_quote")):
            prediction, event, correction = (reversal[key] for key in ("prediction_quote", "event_quote", "correction_quote"))
            return {
                "title": f"{prediction}... {correction}",
                "story": f"{prediction} → {event} → {correction}",
                "hook": prediction, "payoff": correction,
                "hook_evidence": {"kind": "speech", "quote": prediction},
                "event_evidence": {"kind": "speech", "quote": event},
                "payoff_evidence": {"kind": "speech", "quote": correction},
            }
        return self._json(
            "Return JSON only. This is a FINAL EDIT REVIEW for a YouTube Short, not a request to describe "
            "an existing cut. Inspect speech and frames BEFORE AND AFTER the rough cut. Keep the SAME "
            "storyline, or reject it. Do not turn a false prediction into an unrelated gank/kill story. "
            "For a prediction proved wrong: open on the STRONGEST confident false claim (not a weaker "
            "conditional remark earlier), and end on the streamer admitting/correcting his mistake if he "
            "does so nearby. The first appearance, death, or generic reaction is NOT the end of that joke. "
            "For a gameplay highlight: saying 'I nearly killed everyone' or 'I had one pixel' is NOT "
            "proof of a great play. If the images do not show an unmistakable high-impact result or a "
            "genuine funny self-own, reject. Never claim an ult, multikill, win or death unless visibly "
            "confirmed by the provided images. A weak moment cannot be saved by a dramatic title. "
            "Return {\"reject\":true} if this story would not stop a stranger scrolling. Otherwise "
            "choose the strongest self-contained 8-90 second edit of the SAME story. Return title, story, hook, payoff, "
            "hook_evidence and payoff_evidence. For speech evidence give kind='speech', time and a SHORT "
            "EXACT quote copied from the supplied timestamped words. For a visual change give kind='visual', "
            "before_time, time and detail of the change in the supplied timestamped images. Both pieces "
            "of evidence must be distinct and chronological. Times are seconds from game start. "
            f"Rough candidate: {json.dumps(candidate, ensure_ascii=False)}. "
            f"Words: {json.dumps(words, ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames], model=self.shorts_model,
        )

    def audit_short(self, candidate: dict, words: list[dict], frames: list[dict]) -> dict:
        if self.mock:
            return {"keep": True}
        return self._json(
            "Return JSON only. HARSH SHORTS QUALITY GATE. These timestamped frames span the ENTIRE "
            "proposed final edit in order. Judge only what happens INSIDE this cut, not the title or "
            "descriptions of action before it. An after-the-fact claim about almost killing everyone "
            "while the footage shows walking is NOT a highlight. A routine near-death or lost summoner "
            "spell is not a payoff. Keep if there is an actual exceptional on-screen play OR a genuinely "
            "funny contradiction where the speaker first predicts X, X happens, then the speaker admits "
            "the mistake IN THIS CUT. An earlier insult about a named teammate can also pay off if that "
            "teammate visibly survives and helps win a decisive fight while the streamer is dead; a "
            "team ace does NOT prove who got each kill. Transient banners must be VISIBLE in a supplied "
            "frame at the cited timestamp, not inferred from a later push. Ignore repeated single-letter transcript garbage. "
            "Otherwise reject. Return {\"keep\":true/false,\"reason\":\"one sentence\"}. "
            f"Candidate: {json.dumps(candidate, ensure_ascii=False)}. "
            f"Words in cut: {json.dumps(words, ensure_ascii=False)}. "
            f"Frames in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames], model=self.shorts_model,
        )

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
