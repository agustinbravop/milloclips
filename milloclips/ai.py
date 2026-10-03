from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from openai import OpenAI

from .io import read_json, write_json


STATES = ["OTHER", "QUEUE", "CHAMP_SELECT", "LOADING", "IN_GAME", "POST_GAME"]
BEAT_TYPES = ["SETUP", "COMBAT", "REACTION", "STRATEGY", "FARMING", "TRAVEL", "SHOPPING", "DEAD", "RESULT"]
SHORT_MAX_SECONDS = 90
THUMBNAIL_HOOKS = {"high-stakes/amazing moment", "unusual pick/role/challenge", "bold claim/strategy", "extremely good performance", "funny mistake/reversal"}
THUMBNAIL_FILLER = {"a", "al", "como", "con", "de", "el", "en", "es", "esto", "fucking", "fck", "jugué", "jugue", "la", "locura", "me", "mi", "mí", "mira", "momento", "muy", "para", "pick", "que", "rarísimo", "si", "un", "una", "va", "volando", "yo", "he", "ha", "lo", "los", "las", "este", "esta", "esto", "qué", "pero", "y"}
THUMBNAIL_ACTOR = {"jugué", "jugue", "jugando", "me", "mi", "mí", "mío", "mio", "yo"}


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-záéíóúüñ0-9]+", value.casefold()))


THUMBNAIL_PREFERENCES = Path(__file__).resolve().parent.parent / "assets" / "thumbnails" / "preferences.md"


def thumbnail_preferences(vod_dir: Path | None = None) -> str:
    channel = THUMBNAIL_PREFERENCES.read_text(encoding="utf-8") if THUMBNAIL_PREFERENCES.exists() else ""
    local = vod_dir / "thumbnail-feedback.reviewed.md" if vod_dir else None
    if local and local.exists():
        return channel + "\n\nReview notes for THIS VIDEO ONLY:\n" + local.read_text(encoding="utf-8")
    return channel


def thumbnail_copy(value: str) -> str:
    return re.sub(r"\b(?:en\s+)?(?:apoyo|soporte)\b", lambda match: "SUPPORT" if match[0].isupper() else "support", value, flags=re.IGNORECASE)


class AI:
    def __init__(self, vod_dir: Path, model: str = "gpt-5-nano", mock: bool = False, editorial_model: str | None = None):
        self.vod_dir = vod_dir
        self.model = model
        self.editorial_model = editorial_model or model
        self.mock = mock
        self.client = None if mock else OpenAI(api_key=self._key())

    @staticmethod
    def _key() -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY is required for real analysis; use --mock for a local fixture run")
        return key

    def _json(self, prompt: str, images: list[Path] | None = None, *, model: str | None = None, detail: str = "low",
              image_labels: list[str] | None = None) -> Any:
        model = model or self.model
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image_labels is not None and len(image_labels) != len(images or []):
            raise ValueError("Each image must have exactly one label")
        for index, image in enumerate(images or []):
            if image_labels is not None:
                content.append({"type": "text", "text": image_labels[index]})
            encoded = base64.b64encode(image.read_bytes()).decode()
            mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": detail},
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
                prompt=("Spanish League stream with occasional English phrases and gaming terms. "
                        "Transcribe each phrase in its spoken language; never translate English into Spanish. "
                        + ", ".join(vocabulary))[:800],
            )
        return result.model_dump()

    def punctuate_captions(self, words: list[dict]) -> dict:
        tokens = [str(word["word"]).strip() for word in words]
        if self.mock:
            return {"text": " ".join(tokens)}
        return self._json(
            "Restore punctuation and capitalization for these timed dialogue words. The streamer speaks "
            "mainly Spanish but sometimes ENGLISH words, phrases and gaming terms. Preserve each word "
            "in its spoken language, including English phrases such as Oh my God, one shot or tracking. "
            "NEVER translate, paraphrase, correct vocabulary, add, remove, merge or split words. "
            "Return the complete dialogue as one punctuated text string. Only punctuation/case may change. "
            "Restore closing ! and ? at actual exclamation/question endings, using full sentence context "
            "and speech pauses, not at every caption-sized chunk. For this caption style omit opening ¡ and ¿. "
            "Spanish colloquial or rhetorical questions still need a closing ?, even when the rough "
            "transcript omitted the opening mark or accents. Recognize constructions such as pa que, "
            "para que, por que and que quiero from context; do not flatten questions into statements "
            "ending with a period. Shouted interjections should retain their closing !. "
            'Return JSON {"text":"Se queman todos! Oh my God!"} using the actual supplied words. '
            + json.dumps(words, ensure_ascii=False), model=self.model,
        )

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

    def rank_games(self, games: list[dict]) -> list[dict]:
        if self.mock:
            return sorted(
                [
                    {"game_id": game["game_id"], "score": sum(int(beat.get("importance", 1)) for beat in game["beats"]),
                     "reason": "Synthetic ranking from beat importance"}
                    for game in games
                ],
                key=lambda item: item["score"], reverse=True,
            )
        evidence = []
        for game in games:
            beats = sorted(game["beats"], key=lambda beat: beat.get("start", 0))
            highlights = sorted(beats, key=lambda beat: (bool(beat.get("keep")), beat.get("importance", 1)), reverse=True)[:18]
            anchors = [beats[round(index * (len(beats) - 1) / 11)] for index in range(12)] if beats else []
            selected = {beat["id"]: beat for beat in [*highlights, *anchors]}
            evidence.append({
                "game_id": game["game_id"],
                "duration_seconds": game["duration"],
                "beats": [
                    {key: beat.get(key) for key in ("start", "end", "type", "importance", "keep", "text", "reason")}
                    for beat in sorted(selected.values(), key=lambda beat: beat.get("start", 0))
                ],
            })
        parsed = self._json(
            "Rank these League games for a coherent 9-12 minute Spanish YouTube edit. "
            "Score each from 0 to 100 for story potential, humor/emotion, standout moments, "
            "and editability. Prefer a clear arc and varied meaningful beats; penalize routine "
            "or incoherent footage. Judge only supplied evidence. Return JSON only: "
            '{"games":[{"game_id":"game-001","score":85,"reason":"specific evidence-based reason"}]}. '
            "Include every game exactly once. Evidence: " + json.dumps(evidence, ensure_ascii=False),
            model=self.editorial_model,
        )
        write_json(self.vod_dir / "game-ranking.raw.json", parsed)
        by_id = {game["game_id"] for game in games}
        ranked = {}
        for item in parsed.get("games", []):
            if not isinstance(item, dict) or item.get("game_id") not in by_id:
                continue
            try:
                score = float(item["score"])
                if not math.isfinite(score):
                    continue
                score = max(0, min(100, score))
            except (KeyError, TypeError, ValueError):
                continue
            ranked[item["game_id"]] = {
                "game_id": item["game_id"], "score": score,
                "reason": str(item.get("reason", ""))[:500],
            }
        if len(ranked) != len(games):
            raise ValueError("Editorial game ranking did not return a valid score for every game")
        return sorted(ranked.values(), key=lambda item: item["score"], reverse=True)

    def propose_long_intro(self, events: list[dict], story: str, limits: dict, *, feedback: str | None = None) -> dict:
        if self.mock:
            setup = [event for event in events if event["setup_eligible"]][:3]
            return {"kind": "setup", "event_ids": [event["id"] for event in setup], "reason": "Synthetic game premise"}
        return self._json(
            "Return JSON only. Choose an introduction BEFORE the icon card for League/ElmilloR fans. Prefer relevant champion-select "
            "thinking or early-game speech explaining the champion, mode, build, unusual mechanic or challenge we "
            "are entering. Do not choose unrelated pregame banter. Use kind='setup' and 1-4 setup_eligible event IDs; "
            "the card then skips loading/preparation and the main edit starts just BEFORE the first action. Include "
            "enough of the actual premise to replace those pre-action scenes, with no padding or repeated questions. "
            "Alternatively use kind='cold_open' and ONE combat event containing an actually COOL SUCCESSFUL play by "
            "ElmilloR himself. NEVER a death, blunder, lost fight, failed prediction, teammate highlight while he "
            "spectates, or merely his reaction to someone else's play. A cold open needs visible success, not a boast. "
            "If no such play is evidenced choose setup. Return {\"kind\":\"setup\",\"event_ids\":[0,1],\"reason\":\"...\"}. "
            f"Story: {story}. Edited-duration limits: {json.dumps(limits)}. Events: {json.dumps(events, ensure_ascii=False)}. "
            + (f"Previous decision failed validation; repair using setup, not a failed play: {feedback}" if feedback else ""),
            model=self.editorial_model,
        )

    def refine_long_intro(self, proposal: dict, events: list[dict], words: list[dict], frames: list[dict], limits: dict) -> dict:
        if self.mock:
            remaining = min(30, limits["setup"][1])
            parts = []
            for event in events:
                length = min(remaining, event["end"] - event["start"])
                if length > 0:
                    parts.append({"event_id": event["id"], "start": event["start"], "end": event["start"] + length})
                    remaining -= length
            return {**proposal, "segments": parts,
                    "subject": "elmiillor", "outcome": "setup", "complete": True, "related_to_selected_game": True,
                    "evidence": {"kind": "speech", "time": events[0]["start"], "quote": "synthetic setup"}}
        return self._json(
            "Return JSON only. Precisely edit and VERIFY this pre-card intro. Times are seconds from GAME start; negative times are "
            "champion select. For setup preserve complete relevant premise sentences, remove silence/loading/waits, "
            "and use up to FOUR ranges within the corresponding allowed event. The intro replaces all pre-action "
            "setup in the main edit, so include champion/build/game context needed for its first action. "
            "For cold_open use ONE 5-20s range, start BEFORE the cool play, retain its actual successful payoff; "
            "ElmilloR must perform it, not be dead or spectating. Explicitly reject fails, deaths, losses and "
            "unproved boasts. If success is not VISIBLY established return {\"reject\":true,\"reason\":\"...\"}; "
            "a memorable fail is STILL disallowed. For setup do not invent a successful play. "
            "Respect the supplied edited-duration limits; don't pad routine footage to meet them. Choose source "
            "frame or word boundaries, never split speech. Return kind, subject='elmiillor', outcome='setup' or "
            "'success', cool_play=true ONLY for a successful play, complete=true, related_to_selected_game=true, "
            "reason and segments [{event_id,start,end}]. Supply evidence={kind:'speech',time,quote} with an exact "
            "relevant setup quote for setup; or {kind:'visual',time,detail} describing ElmilloR's VISIBLE success "
            "for cold_open. The evidence must be inside retained footage. Only cite supplied image timestamps. "
            f"Proposal: {json.dumps(proposal, ensure_ascii=False)}. Allowed events: {json.dumps(events, ensure_ascii=False)}. "
            f"Limits: {json.dumps(limits)}. Words: {json.dumps(words, ensure_ascii=False)}. "
            f"Frame times: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames], model=self.editorial_model, detail="high",
        )

    def plan_long(self, beats: list[dict], *, budget_feedback: dict | None = None) -> dict:
        if self.mock:
            selected = [beat for beat in beats if beat["keep"]]
            # Mock fixture chooses a bounded continuous story, without padding real edits.
            if selected:
                start = selected[0]["start"]
                selected = [beat for beat in beats if start <= beat["start"] < start + 565]
            scenes = []
            for beat in selected:
                if scenes and beat["id"] == scenes[-1]["end_beat_id"] + 1:
                    scenes[-1]["end_beat_id"] = beat["id"]
                else:
                    scenes.append({"start_beat_id": beat["id"], "end_beat_id": beat["id"], "purpose": "synthetic scene"})
            return {
                "selected_ids": [beat["id"] for beat in selected],
                "scenes": scenes,
                "cold_open_beat_id": selected[-1]["id"] if selected else None,
                "story": "Synthetic game rough cut.",
            }
        compact = [
            {key: beat.get(key) for key in ("id", "start", "end", "phase", "type", "importance", "keep", "text", "reason")}
            for beat in beats
        ]
        parsed = self._json(
            "Edit this Spanish League game into a compelling chronological YouTube story. Select COMPLETE SCENES "
            "targeting 10-12 minutes INCLUDING intro and channel cards when enough meaningful content exists "
            "(roughly 9:40-11:40 of scenes). Do not shorten a rich game just to hit ten minutes or pad a thin game. "
            "Every scene must contain a concrete "
            "premise, setup, action, outcome, joke, reversal, or reaction; never add footage merely to cover equal "
            "amounts of early/mid/late game. Exclude safe recalls, walking back to lane, routine farming, shopping, "
            "respawn waits, repeated explanations, and action whose result is not included. Routine shopping is "
            "different from a meaningful build decision: retain item/augment choices, analysis, the actual selection "
            "and its effect so viewers can track the champion's power-ups before the next fight. "
            "For round-based modes such as Arena, preserve the first rounds in order, including the second round; "
            "do not silently jump from the first fight to a later upgrade. Prefer complete rounds over isolated "
            "highlights; omit a later repetitive round only when no build/stakes/story development is lost. "
            "Keep relevant host/map/mechanic changes and decision-making as useful breathers between fights; "
            "alternate action and meaningful preparation naturally, while allowing exciting action-to-action cuts. "
            "Reject tiny isolated fragments with no standalone payoff: include their consequential follow-up "
            "decision/result or omit the fragment. Preserve short setup "
            "before each action and its immediate outcome/reaction afterward. Include the pick/build premise, the "
            "strongest varied moments, escalating stakes, and the actual final fight/result. The ending includes "
            "the resolved defeat/victory/placement screen, complete final dialogue and at least one second without "
            "streamer speech before the end card, usually by lingering another one or two seconds. A pending "
            "combat/waiting screen is not a final result. Do not start or end a "
            "scene mid-sentence. Scene ranges use inclusive adjacent beat IDs and should usually last 20-90 seconds. "
            "Mark the actual story premise and ending required=true; optional scenes must be removable without "
            "orphaning a setup or payoff. Select pregame speech only if relevant to this story; do not force it. "
            "Nominate a successful ElmilloR play as cold_open_beat_id, or an opening premise beat when no cool "
            "successful play exists. Never nominate his failure/death. This is only an intro hint: a separate pass "
            "will decide setup versus successful-play cold open. A later teaser remains in its normal "
            "chronological scene. ElmilloR himself must be the active subject through his play, call, joke, or reaction; "
            "never choose a teammate highlight while he is dead or spectating. Return JSON only: "
            '{"scenes":[{"start_beat_id":0,"end_beat_id":3,"purpose":"specific story job","required":true}],'
            '"cold_open_beat_id":3,"story":"one sentence"}. Beats:\n'
            + json.dumps(compact, ensure_ascii=False)
            + ("\nRevise the selection using actual refined durations below. Preserve every required scene range "
               "and the cold-open scene. Keep approved ranges unchanged; add/remove optional COMPLETE story units "
               "to meet the footage budget. Do not add filler or merely lengthen ranges. Feedback: "
               + json.dumps(budget_feedback, ensure_ascii=False) if budget_feedback else ""), model=self.editorial_model
        )
        scenes = parsed.get("scenes", []) if isinstance(parsed.get("scenes"), list) else []
        selected_ids = [
            beat["id"]
            for scene in scenes if isinstance(scene, dict)
            for beat in beats
            if type(scene.get("start_beat_id")) is int and type(scene.get("end_beat_id")) is int
            and scene["start_beat_id"] <= beat["id"] <= scene["end_beat_id"]
        ]
        return {
            "scenes": scenes,
            "selected_ids": selected_ids,
            "cold_open_beat_id": parsed.get("cold_open_beat_id"),
            "story": str(parsed.get("story", "")),
        }

    def refine_long_scene(self, scene: dict, beats: list[dict], words: list[dict], frames: list[dict], *, cold_open: bool) -> dict:
        if self.mock:
            parts = scene.get("located_segments") or [{"start": scene["start"], "end": scene["end"]}]
            if not scene.get("located_segments") and scene["start"] < 0 < scene["end"]:
                parts = [{"start": scene["start"], "end": 0}, {"start": 0, "end": scene["end"]}]
            answer = {"segments": [
                {**part, "action_start": part["start"] + min(2, (part["end"] - part["start"]) / 4),
                 "outcome_time": part["end"] - 1, "reaction_end": part["end"] - 0.5,
                 "downtime_start": None, "downtime_kind": "none", "event_kind": "speech" if part["end"] <= 0 else "combat",
                 "start_reason": "Synthetic pre-action setup", "end_reason": "Synthetic completed reaction",
                 "evidence": {}} for part in parts
            ], "needs_earlier_context": False, "needs_later_context": False}
            if cold_open:
                last = answer["segments"][-1]
                answer.update({"cold_open_start": max(last["start"], last["end"] - 10), "cold_open_end": last["end"]})
            return answer
        if scene.get("format") == "short":
            return self._json(
                "Return JSON only. Edit this selected SHORT scene using these ordered images and spoken words. Identify "
                "the COMPLETE event, not just the streamer's exclamation or death. For combat, start "
                "before engagement and end only after the relevant fight/round visibly resolves and "
                "its useful reaction finishes. Continue spectating living teammates after the streamer "
                "dies. In Arena, a death recap or respawn timer is NOT a round ending; look for all "
                "relevant combat ending, the round result or return to preparation. For a premise, keep "
                "the complete concise choice explanation, not other shopping/choices. Use supplied "
                "word timings to preserve sentences, including occasional English speech. "
                "Speech-only moments, including between-game banter, need a complete premise, punchline "
                "and reaction from the supplied words, not a combat outcome. Use event_kind='speech'. "
                "In boundary_pass=precise, keep the located events and segment count; start/end must "
                "stay in their matching boundary_windows. Refine those cuts, do not rediscover a different story. "
                f"The full short may last up to {SHORT_MAX_SECONDS} seconds; remaining_seconds is the available budget. "
                "Give the streamer's meaningful spoken setup and useful payoff/reaction room to finish. "
                "Do not compress a better story just to make a 20-45 second clip. Preserve streamer_evidence "
                "and its complete spoken sentence; game announcer lines and chat are not the streamer speaking. "
                "Remove idle time with up to four chronological segments, never truncate unresolved "
                "action to make it shorter. Frame indices below refer to THIS supplied image list, not "
                "timestamps. Return only integer frame indices for boundaries/anchors; code converts "
                "them to exact times. If the first images already show the chosen action underway or "
                "the final images lack its resolved outcome, set needs_earlier_context or needs_later_context. "
                "validation_feedback describes a previous construction issue: repair the SAME story. "
                "Return JSON with boolean needs_earlier_context and needs_later_context, plus segments. "
                "Each segment needs integer start_frame, action_frame, outcome_frame, reaction_frame, end_frame; "
                "event_kind (combat or speech); start_reason, end_reason, action_detail, outcome_detail and "
                "reaction_detail. Descriptions must name concrete visible states and changes in the actual "
                "images, not generic placeholders such as 'visible engagement' or 'round resolved'. "
                "For combat, outcome_detail must describe the visible proof that ALL relevant combat "
                "has ended. A key death, a turning point or chaotic fighting does not qualify. If there "
                "is no such image, return needs_later_context=true rather than inventing completion. "
                "Choose the actual supplied indices. expected_event_kind identifies "
                "the selected event. known_events are validated long-video findings for orientation. "
                "verified_evidence gives exact selected hook/payoff anchors, including transient banners. "
                "Keep those moments; inspect their supplied frames rather than replacing a visible payoff "
                "with a generic chat reaction. A remote kill banner does not prove the camera showed that "
                "fight, and an enemy double kill is not the streamer's victory. Do not invent dialogue "
                "from chat or captions; only supplied spoken words support speech. "
                "Scene: " + json.dumps(scene, ensure_ascii=False)
                + ". Words: " + json.dumps(words, ensure_ascii=False)
                + ". Frame index/time: " + json.dumps([{ "index": index, "time": frame["timestamp"]} for index, frame in enumerate(frames)]),
                [Path(frame["path"]) for frame in frames], model=self.editorial_model, detail="high",
                image_labels=[f"Frame index {index}; game-relative time {frame['timestamp']:.6f} seconds" for index, frame in enumerate(frames)],
            )
        return self._json(
            "Return JSON only. Edit this selected scene for League/ElmilloR fans. Timestamps are seconds from GAME START. "
            "These are timestamped decoded stills, NOT continuous video. In boundary_pass='locate', identify complete "
            "events and rough retained ranges. In boundary_pass='precise', use the dense boundary windows to choose "
            "exact entry/exit points; keep the same events, segment count and storyline. Use 1-6 chronological "
            "non-overlapping segments to remove INTERNAL downtime, not just the scene's outer tails. Never merge "
            "two fights through recall/shop/return travel. Return needs_earlier_context=true if the earliest frame "
            "already shows the selected action underway, and needs_later_context=true if outcome/reaction is unresolved. "
            "Do not invent a boundary when context is missing. Start before the action, normally with TWO seconds "
            "of approach/target call; preserve a necessary setup sentence from its beginning. Finish the actual "
            "outcome and immediate useful reaction. For Summoner's Rift cut at recall ONSET when dialogue is complete; "
            "omit the channel, routine shopping and walk back. For Arena remove idle between-round waiting, "
            "but RETAIN meaningful augment/item choices and their complete analysis/selection, plus relevant "
            "host/map/mechanic changes: these are build progression and useful breathers, not downtime. "
            "Do not trim a scene to a tiny inconsequential fragment when its following power-up decision gives "
            "it purpose. Separate a choice and fight only to remove actual idle time; preserve both complete events. "
            "For final_scene=true, retain the actual resolved victory/defeat/placement (not a waiting-for-combat "
            "screen), finish final dialogue and leave at least ONE second free of streamer speech before the "
            "end card. Choose the EARLIEST natural one-second speech pause after the resolved result and immediate "
            "reaction. Do not chase every later remark: omit renewed post-game review, repeated result complaints "
            "and next-match talk once a complete ending and pause exist. Allow a short result-screen linger "
            "instead of applying routine downtime trimming there. "
            "A recall can be cancelled: do not discard a meaningful renewed fight. "
            "Keep useful speech during recall/travel until its complete sentence/joke finishes, then cut. "
            "Speech-only banter/setup is valid: event_kind='speech', action_start means premise-line start and "
            "outcome_time means punchline/explanation. Never require a visual fight for a conversation. "
            "Each segment needs start/end, action_start, outcome_time, reaction_end, downtime_start (null if none), "
            "downtime_kind ('recall','shopping','travel','waiting','none'), event_kind ('combat' or 'speech'), "
            "start_reason/end_reason, and evidence for action_start/outcome_time/reaction_end and any downtime_start. "
            "Each evidence object has kind='visual', time equal to a SUPPLIED image timestamp, detail of what is visible; "
            "or kind='speech', time and an EXACT quote from timestamped words. Anchor times must equal evidence times. "
            "reaction_end denotes the last necessary reaction anchor; retain its whole spoken phrase. "
            "In precise pass start/end must stay in their corresponding boundary_windows AND allowed_region, "
            "and cold_open_start/end must stay in cold_open_windows. Choose inspected frame/word times, not guessed decimals. "
            "If completion is outside a dense window request more context instead of guessing. "
            + ("Also choose the complete most amazing or punchy 5-20 second ElmilloR moment as "
               "cold_open_start/cold_open_end. ElmilloR himself must be the active subject through his play, call, joke, "
               "or reaction; never choose a teammate highlight while he is dead or spectating. It must contain visible "
               "action or an immediately compelling line and its payoff; never choose walking, waiting, setup without "
               "action, or an uneventful reaction. " if cold_open else "")
            + 'Return {"needs_earlier_context":false,"needs_later_context":false,"segments":'
            '[{"start":12.0,"end":45.0,"action_start":14.0,"outcome_time":40.0,"reaction_end":43.0,'
            '"downtime_start":45.0,"downtime_kind":"recall","event_kind":"combat",'
            '"start_reason":"approach before engagement","end_reason":"recall begins after reaction",'
            '"evidence":{"action_start":{"kind":"visual","time":14.0,"detail":"engagement begins"},'
            '"outcome_time":{"kind":"visual","time":40.0,"detail":"fight resolves"},'
            '"reaction_end":{"kind":"speech","time":43.0,"quote":"exact words"},'
            '"downtime_start":{"kind":"visual","time":45.0,"detail":"recall channel begins"}}}]'
            + (",\"cold_open_start\":30.0,\"cold_open_end\":37.0" if cold_open else "")
            + "}. Scene: " + json.dumps(scene, ensure_ascii=False)
            + ". Beats: " + json.dumps(beats, ensure_ascii=False)
            + ". Timestamped words: " + json.dumps(words, ensure_ascii=False)
            + ". Supplied frame timestamps: " + json.dumps([frame["timestamp"] for frame in frames]),
            [Path(frame["path"]) for frame in frames], model=self.editorial_model, detail="high" if scene.get("boundary_pass") == "precise" else "low",
            image_labels=[f"Frame index {index}; game-relative time {frame['timestamp']:.6f} seconds" for index, frame in enumerate(frames)],
        )

    def thumbnail_concepts(self, beats: list[dict], story: str, images: list[Path], force: bool = False, references: list[dict] | None = None, reactions: list[dict] | None = None) -> list[dict]:
        if self.mock:
            choices = [("ME PASÓ ESTO", "action", "shocked")]
            return [
                {"title": headline.title(), "headline": headline, "hook": "high-stakes/amazing moment",
                 "template": template, "expression": expression, "beat_id": beats[index % len(beats)]["id"]}
                for index, (headline, template, expression) in enumerate(choices)
            ] if beats else []
        evidence = [{key: beat.get(key) for key in ("id", "type", "importance", "text", "reason")} for beat in beats]
        prompt = """
Create exactly ONE strong Spanish YouTube title/thumbnail concept for this FINAL League edit.
Think punchy League creator packaging: expressive streamer face, one dominant game subject,
very short readable copy. Choose the strongest hook from this video's evidence.
Each becomes a Sunburst-generated composition with an exact real reaction photo and optional exact icons.
Think cinematically staged, game-faithful Rift/character artwork, a relevant build or statistic,
or a simpler close-perspective Rift backdrop and exceptionally strong headline. No mandatory slots.
Use reference examples for visual language, never copy their content or people's faces.
Headlines must be natural Spanish, never keyword soup.
Name the hook's concrete subject, mechanic or consequence. Reject interchangeable labels like
ELECCIÓN INSÓLITA, PICK RARO or QUÉ LOCURA; they do not tell viewers what is special about this video.
Write titles as something a Spanish-speaking League creator would actually say, not compressed
keywords. Prefer plain, direct language over clever phrasing. Before returning, read the title and
headline aloud mentally and fix unnatural grammar or word combinations. Ground both in this video.
Package a reason to watch, not merely a label for a mechanic or lesson. First-person copy is optional: the face already identifies
ElmilloR. The title and thumbnail headline must work together to raise a specific viewer question.
Create curiosity through a concrete statement, not default question copy. Video titles are FULL CAPS,
more descriptive than the 2-4-word thumbnail headline, with relevant action/pick/role/build/stakes.
Use Thebausffs/Nemesis-style directness, with some Drututt influence; do not repeat the headline.
Identify the relevant champion from each concept's evidence. Do not require all three to feature
the same champion. A teammate's play is not ElmilloR's achievement. Images match evidence list order.
Include a canonical Riot champion asset name only when identified and relevant; never invent it.
Optional assets: up to six objects {"kind":"item"|"rune","key":"numeric Riot ID"}.
Only request an icon when the supplied evidence establishes that exact item/rune;
omit uncertain IDs. Do not add unrelated icons as decoration.
Include the exact evidenced item/rune icon when it is the defining reason the hook works.
For hook, use EXACTLY one of these strings: "high-stakes/amazing moment",
"unusual pick/role/challenge", "bold claim/strategy", "extremely good performance", "funny mistake/reversal".
A strategy can claim it works only when the evidence proves its payoff. Do not turn a hypothetical
("podría", "casi", "si") into an achieved result. Headlines are 2-4 Spanish words, all-caps-friendly,
punchy editorial paraphrases. Claims must be supported by the cited evidence, but natural Spanish
paraphrases and grammar are encouraged. Relate the story to Elmiillor's actual experience;
never attribute an enemy or teammate's achievement to him. Titles complement rather than repeat
the headline. Use template action, identity, or strategy.
Available expressions: shocked, hold-on, overly-hyped, smiling. Prefer smiling for positive/playful
hooks and normal excitement. Questioning copy needs curiosity or skepticism, never overly-hyped.
Reserve overly-hyped for RARE, genuinely extreme emotional peaks:
ordinary wins or strong plays are not enough. Explain the exceptional evidence and why smiling
would undersell it if selecting overly-hyped. Still use shock or skepticism when the actual hook
calls for them; do not force smiles onto disasters. Cite a supplied beat_id. Return JSON only:
Each concept must include evidence: an exact short phrase copied from its cited beat's spoken text.
If a supplied beat cannot support a concise truthful concept, omit it rather than guessing.
Each concept MUST include a nonempty reaction_reason explaining the chosen photo's emotional fit
to the evidence and copy. Before returning, check all three have reaction_reason, a 2-4-word
headline (at most 40 characters), and an exact evidence quote from their cited beat.
{"concepts":[{"title":"...","headline":"...","hook":"...","template":"action","background":"gameplay","expression":"shocked","reaction_reason":"Why this real photo matches the evidenced hook and copy","beat_id":1,"evidence":"exact copied phrase","champion":"","assets":[]}]}
        Story: """ + story + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False)
        preferences = thumbnail_preferences(self.vod_dir)
        prompt += "\nPersistent human thumbnail feedback (apply to all candidates):\n" + preferences
        if references:
            prompt += "\nStyle reference images follow the gameplay evidence images. Learn the visual approach, not their subjects.\n" + json.dumps(references, ensure_ascii=False)
        if reactions:
            prompt += (
                "\nAfter the style references come the AVAILABLE REAL REACTION PHOTOS, in catalog order. "
                "Choose expression ONLY from this catalog by inspecting the actual eyes, mouth, hands and intensity. "
                "For each concept include reaction_reason explaining how that photo fits this specific evidence, "
                "headline, emotional promise and viewer question. Never choose overly-hyped just because it is a build or win. "
                "Use smiling more often for positive concepts; overly-hyped needs evidence of an exceptional emotional peak. "
                "Prefer a calm positive smile for confident/playful hooks, not closed-eye shouting by default. "
                "Do not enforce artificial variety if two concepts genuinely need the same emotion. "
                "Do not invent an unavailable pose. Catalog: " + json.dumps(reactions, ensure_ascii=False)
            )
        reference_images = [Path(reference["path"]) for reference in references or []]
        reaction_images = [Path(reaction["path"]) for reaction in reactions or []]
        concept_images = [*images, *reference_images, *reaction_images]
        model = getattr(self, "editorial_model", getattr(self, "model", "gpt-5-nano"))
        signature = hashlib.sha256((prompt + model).encode() + b"".join(image.read_bytes() for image in concept_images)).hexdigest()
        cache_path = self.vod_dir / "thumbnail-concepts.cache.json"
        cache = read_json(cache_path) if cache_path.exists() and not force else {}
        if cache.get("signature") != signature:
            cache = {"signature": signature}
        parsed = cache.get("concepts")
        if parsed is None:
            parsed = self._json(prompt, concept_images, model=model, detail="high")
            cache["concepts"] = parsed
            write_json(cache_path, cache)
        write_json(self.vod_dir / "thumbnail-concepts.raw.json", parsed)
        audit = cache.get("audit") or self._json(
            "Audit the single proposed League thumbnail concept against supplied evidence. Natural Spanish "
            "paraphrases are allowed; reject unsupported entities, outcomes, teammate plays attributed to ElmilloR, "
            "unnatural copy, unsupported assets, hypothetical claims presented as achieved, and emotionally "
            "misleading reaction photos. Return JSON {\"reviews\":[{\"index\":0,\"accepted\":true,\"reason\":\"...\"}]}. "
            "Concepts: " + json.dumps(parsed.get("concepts", []), ensure_ascii=False)
            + " Evidence: " + json.dumps(evidence, ensure_ascii=False)
            + " Preferences: " + preferences + " Reaction-photo catalog: " + json.dumps(reactions or [], ensure_ascii=False),
            [*images, *reaction_images], model=model,
        )
        write_json(self.vod_dir / "thumbnail-concepts.audit.raw.json", audit)
        cache["audit"] = audit
        write_json(cache_path, cache)
        accepted = {review.get("index") for review in audit.get("reviews", []) if review.get("accepted") is True}
        valid_ids = {beat["id"] for beat in beats}
        output = []
        by_id = {beat["id"]: beat for beat in beats}
        for index, concept in enumerate(parsed.get("concepts", [])):
            if index not in accepted:
                continue
            if concept.get("beat_id") not in valid_ids:
                continue
            headline = str(concept.get("headline", "")).strip()[:40]
            if not headline or len(headline.split()) > 4:
                continue
            evidence = str(concept.get("evidence", "")).strip()[:160]
            cited = _words(evidence)
            title = str(concept.get("title", headline)).strip()[:100]
            spoken = str(by_id[concept["beat_id"]].get("text", "")).casefold()
            quoted = re.findall(r"[a-záéíóúüñ0-9]+", evidence.casefold())
            # Accept a quote that only differs by joined or split words ("one shot" for "oneshot").
            if not evidence or not quoted or (evidence.casefold() not in spoken
                                              and not re.search(".*?".join(map(re.escape, quoted)), spoken)):
                continue
            if concept.get("hook") not in THUMBNAIL_HOOKS or concept.get("template") not in {"action", "identity", "strategy"}:
                continue
            available = {reaction["expression"] for reaction in reactions} if reactions else {"shocked", "hold-on", "overly-hyped", "smiling"}
            if concept.get("expression") not in available:
                continue
            if reactions and not str(concept.get("reaction_reason", "")).strip():
                continue
            if {"podria", "podría", "casi", "si"} & cited and concept["hook"] in {"high-stakes/amazing moment", "extremely good performance"}:
                continue
            output.append({
                "title": thumbnail_copy(title).upper(),
                "headline": thumbnail_copy(headline).upper(),
                "hook": str(concept.get("hook", "")).strip()[:120],
                "template": str(concept.get("template", "action")).strip(),
                "background": "splash" if concept.get("background") == "splash" else "gameplay",
                "expression": str(concept.get("expression", "shocked")).strip(),
                "reaction_reason": str(concept.get("reaction_reason", "")).strip()[:500],
                "beat_id": concept["beat_id"],
                "evidence": evidence,
                "champion": str(concept.get("champion", "")).strip() if str(concept.get("champion", "")).isalnum() else "",
                "assets": [asset for asset in concept.get("assets", []) if isinstance(asset, dict)
                           and asset.get("kind") in {"item", "rune"}
                            and isinstance(asset.get("key"), str) and asset["key"].isdecimal()][:6],
            })
        return output[:1]

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
        path = self.vod_dir / "short-proposal-windows.json"
        answers = read_json(path).get("answers", {}) if path.exists() else {}
        windows = [evidence[max(0, offset - 4):offset + 24] for offset in range(0, len(evidence), 20)]
        def signature(window):
            return hashlib.sha256(json.dumps({"beats": window, "model": self.editorial_model, "prompt_version": 3}, sort_keys=True).encode()).hexdigest()
        def propose(window):
            return self._json(
                "Return JSON only. Find up to THREE DIFFERENT events in this Spanish stream window that could make a "
                "funny, amazing, or surprising TikTok even to someone who did not watch the stream. "
                "Start with the ACTUAL payoff: a spectacular play, disastrous blunder, unexpected reversal, "
                "or a funny contradiction/confession. Quote its exact speech if present. "
                "An ordinary recall, tactical comment, cooldown, item, objective plan, retreat, or mention of "
                "a kill is NOT an event without an exceptional visible or spoken outcome. A strong rant is valid "
                "only when it has an immediately quotable hook and escalates to a sharper punchline. Between-game banter, "
                "League queue/draft/chat reactions are eligible; casino/slots and other games are not. "
                "Every moment must include meaningful Elmiillor speech, not only silent action, a grunt, "
                "chat text or game announcer audio. Return zero if none. "
                "Beat labels and importance may be wrong. Give the ID of the beat containing the payoff. "
                "Use one continuous scene for a play, or 2-4 chronological scenes only for a real callback. "
                f"Each scene uses inclusive beat IDs; retain complete setup/payoff up to {SHORT_MAX_SECONDS} seconds after trimming. "
                "Return {\"candidates\":[{\"title\":\"...\",\"proposed_payoff\":\"specific event\","
                "\"payoff_beat_id\":2,\"scenes\":[{\"start_beat_id\":1,\"end_beat_id\":2}]}]}. Beats:\n"
                + json.dumps(window, ensure_ascii=False), model=self.editorial_model,
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(propose, window): signature(window) for window in windows if signature(window) not in answers}
            for future in as_completed(pending):
                answers[pending[future]] = future.result()
                write_json(path, {"answers": answers})
        candidates = []
        for window in windows:
            proposed = answers[signature(window)].get("candidates")
            if isinstance(proposed, list):
                candidates.extend(proposed[:3])
        # A separate wide view may find a callback that spans several minutes.
        parsed = self._json(
            "Return JSON only. Find at most TWO surprising or hilarious callbacks across this stream evidence. "
            "An actual ending must overturn or deliver on an earlier premise; no generic game recaps, "
            "plans, or unrelated highlights. Zero is fine. Use 2-4 short chronological scenes, "
            f"retain complete setup/payoff up to {SHORT_MAX_SECONDS} seconds after trimming, and identify the exact payoff beat. "
            "Only League of Legends stories with meaningful streamer speech are eligible. "
            "Return {\"candidates\":[{\"title\":\"...\",\"proposed_payoff\":\"...\","
            "\"payoff_beat_id\":2,\"scenes\":[{\"start_beat_id\":1,\"end_beat_id\":2}]}]}. Beats:\n"
            + json.dumps(evidence, ensure_ascii=False), model=self.editorial_model,
        )
        if isinstance(parsed.get("candidates"), list):
            candidates.extend(parsed["candidates"][:2])
        return candidates

    def visual_shorts(self, frames: list[dict]) -> list[dict]:
        if self.mock:
            return []
        parsed = self._json(
            "Return JSON only. Coarsely scout this stream window, including between-game footage, using ordered still images. "
            "These still images are in time order, not continuous video. Find at most ONE genuinely "
            "exceptional VISUAL Short where the streamer's champion visibly makes a decisive play, or "
            "an unmistakably surprising outcome occurs on screen. A genuine on-camera mishap or unexpected "
            "League-related chat/reveal outside gameplay is equally eligible. Exclude slots and other games. "
            "Speech will be checked separately; prefer a visible streamer reaction. Use before/after frames as evidence. "
            "Do not mistake allies' plays, a routine death, position changes, an ordinary retreat, "
            "or a post-game screen for the streamer's highlight. No invented speech or unseen actions. "
            "Return {\"events\":[{\"start\":1900,\"end\":1930,\"payoff\":1925,"
            "\"description\":\"visible factual event\"}]} or {\"events\":[]}. "
            "Game-relative seconds per image: " + json.dumps([frame["timestamp"] for frame in frames]),
            [Path(frame["path"]) for frame in frames], model=self.editorial_model,
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
            "pick 2-4 BRIEF chronological beat ranges: spoken premise, turn/setback, visual payoff. "
            f"The total selected footage must never exceed {SHORT_MAX_SECONDS} seconds after trimming. Give payoff_beat_id "
            "as the REAL beat ID containing the visible outcome in the final scene, NEVER an example ID. "
            "Return a JSON object with candidates array, each having title, proposed_payoff, "
            "payoff_beat_id, and scenes array of start_beat_id/end_beat_id pairs. "
            "Gameplay frames at game-relative seconds: " + json.dumps([frame["timestamp"] for frame in frames])
            + ". Earlier beat evidence and late beat IDs: " + json.dumps(beats, ensure_ascii=False),
            [Path(frame["path"]) for frame in frames], model=self.editorial_model,
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
            [Path(frame["path"]) for frame in frames], model=self.editorial_model,
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
            "Return JSON only. Verify the factual premise and complete story of this source moment. "
            "Discover a complete factual story independently from the actual images and transcript "
            "inside the supplied search windows. There is no assumed gameplay event or outcome. "
            "Only League of Legends footage and League draft/queue discussion are eligible. Reject "
            "casino/slots or other games. Require meaningful ELMIILLOR speech that contributes a premise, "
            "joke, commentary or reaction. Silent gameplay, grunts, generic exclamations, chat text, "
            "champion voice lines and game announcer audio do not qualify. Do not attribute the game "
            "announcer's objective/round announcements to Elmiillor. Return streamer_evidence as "
            "{kind:'speech',time:<seconds>,quote:<exact substantive streamer phrase from transcript>}. "
            "Distinguish neutral jungle monsters/minions from enemy champions, farming from PvP, "
            "a normal ability animation from an ultimate, and a casino/menu from gameplay. "
            "A claimed teamfight requires identifiable opposing champions and a visible resolved encounter; "
            "a claimed ultimate requires its distinctive visible action, not merely a champion standing in brush. "
            "If those facts are unclear, reject that premise rather than filling gaps from game knowledge. "
            "These proposed scenes are SEARCH WINDOWS, not finished cuts. Find the premise, event, "
            "outcome and useful reaction inside them, as for a complete long-video scene. "
            "Images are timestamped stills, NOT continuous video. Speech is timestamped separately. "
            "Do NOT invent wins, kills or abilities from dialogue. Reject unsupported premises, unrelated "
            "setup/payoff, or missing outcomes. Do not reject because the SEARCH WINDOW opens with "
            "ordinary setup: choose provisional segments opening at a supported hook and ending after "
            "its related payoff/reaction. The code trims to your anchors. Entertainment ranking belongs "
            "to a separate judge, not this verification. Funny exchanges and unusual mechanics may pay "
            "off through speech without an exceptional play or an admission of error. "
            "Return {\"reject\":true,\"reason\":\"one sentence\"} when the story cannot be supported. Otherwise return JSON "
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
            "its proposed scene range. Start with real action/speech; include the complete useful reaction. "
            "Keep 1 scene for a self-contained play, or 2-4 for an arc; never pad to reach a scene count. "
            f"Keep complete spoken setup and useful outcome/reaction; up to {SHORT_MAX_SECONDS} seconds is allowed. "
            "Do not shorten a stronger complete story to hit 20-45 seconds. Long time jumps must be self-evident from repeated "
            "wording, champion identity, or visible context because no editorial time labels will be added. "
            f"Search windows: {json.dumps(proposal['scenes'], ensure_ascii=False)}. "
            f"Transcript: {json.dumps([{key: beat[key] for key in ('start', 'end', 'text') if key in beat} for beat in beats], ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames],
            model=self.editorial_model, detail="high",
            image_labels=[f"Source image at {frame['timestamp']} seconds" for frame in frames],
        )

    def choose_shorts(self, candidates: list[dict], images: list[Path]) -> list[int]:
        if self.mock:
            return list(range(len(candidates)))
        parsed = self._json(
            "Return JSON only. You are the sole editorial judge selecting the best TWO source moments "
            "for YouTube Shorts/TikTok/Reels, BEFORE precise shorts editing. Compare candidates head-to-head "
            "for entertainment, a promising immediate opening, self-contained clarity, payoff and variety. "
            "Use the explicit candidate_id values. Choose two DIFFERENT stories with non-overlapping source footage, "
            "never two versions of the same event. "
            "Rank the strongest pair first, then worthwhile alternatives in preference order. "
            "Only select League of Legends stories with meaningful Elmiillor speech. Prefer his personality, "
            "a complete spoken premise and a satisfying spoken reaction over silent gameplay. "
            "Longer setup/payoff is welcome up to 90 seconds when it makes the story stronger. "
            "A routine draft/build decision, climbing goal, unrelated tactical question, or kill banner "
            "is not an entertaining payoff by itself. Favor funny failed promises, mistakes with a "
            "spoken reaction, unusual mechanics with a humorous explanation, or a complete rant/joke. "
            "Alternatives may be used if a selected moment cannot fit source/duration constraints. Do not penalize rough "
            "boundaries that the editor can trim. Funny exchanges, personality, unusual mechanics, "
            "build consequences, tense plays and reversals are all eligible; not every short needs a "
            "spectacular play or a confession. The supplied images are hook then payoff for each candidate "
            "in order, two images per candidate. Do not reward titles or claims unsupported by evidence. "
            "An expressive facecam or vocal reaction is a bonus, not a requirement. When quality is close, "
            "prefer a varied pair such as one personality/rant clip and one gameplay/reversal clip. "
            "Choose ZERO if none are genuinely entertaining; do not fill a quota. "
            "Return {\"selected_ids\":[0,1,2],\"reason\":\"why this pair is best and alternatives are ranked\"}. "
            "Candidate evidence (zero-based IDs): " + json.dumps(candidates, ensure_ascii=False),
            images,
            model=self.editorial_model,
        )
        return parsed.get("selected_ids", []) if isinstance(parsed.get("selected_ids"), list) else []

    def refine_short(self, candidate: dict, words: list[dict], frames: list[dict]) -> dict:
        if self.mock:
            return {
                "title": candidate["title"], "story": candidate["reason"],
                "hook": candidate["hook"], "payoff": candidate["payoff"],
                "hook_evidence": candidate["hook_evidence"], "payoff_evidence": candidate["payoff_evidence"],
                "segments": [{"start": item["relative_start"], "end": item["relative_end"]} for item in candidate.get("segments", [])],
            }
        # One clock for the editor: game-relative evidence, never absolute VOD offsets.
        evidence = {key: value for key, value in candidate.items() if key not in {"source_start", "source_end", "_images"}}
        for key in ("segments", "rough_segments", "avoid_segments"):
            if key in evidence:
                evidence[key] = [{"start": item["relative_start"], "end": item["relative_end"]} for item in evidence[key]]
        return self._json(
            "Return JSON only. Edit this source moment ALREADY SELECTED by the shorts judge. "
            "Do not reconsider its entertainment value; your job is precise, self-contained cutting. "
            "Inspect speech and frames BEFORE AND AFTER the rough cut. Keep the SAME "
            "storyline. Do not turn a false prediction into an unrelated gank/kill story. "
            "For a prediction proved wrong: open on the STRONGEST confident false claim (not a weaker "
            "conditional remark earlier), and end on the streamer admitting/correcting his mistake if he "
            "does so nearby. The first appearance, death, or generic reaction is NOT the end of that joke. "
            "Never claim an ult, multikill, win or death unless visibly confirmed by the provided images. "
            "Preserve the necessary premise, actual outcome and immediate useful reaction, including "
            "item/augment decisions when they explain the consequence. Open on the strongest supported "
            "line or action, finish useful dialogue, remove idle time and repetitions, and never pad. "
            "Solve word boundaries, sentence completion and duration WHILE choosing cuts. If editing_feedback "
            "is present, it identifies a construction problem in the previous attempt: use this expanded "
            "context to FIX the SAME selected short, not to judge or reject it. avoid_segments identifies "
            "the other short's footage; remove unnecessary duplicated setup. Do not copy entire context "
            "windows as cuts; select meaningful entry/exit points at speech pauses. "
            "Choose a self-contained "
            f"edit of the SAME story. The ONLY duration requirement is 8-{SHORT_MAX_SECONDS} seconds TOTAL retained footage. "
            "Preserve meaningful streamer speech and complete setup/reaction; do not rush toward a 20-45 second target. "
            "A complete 12- or 17-second moment is valid; do not pad it. For a multi-scene candidate, "
            "return the same number of chronological segments, each inside its corresponding supplied "
            "context segment. rough_segments, when present, are the original cuts; segments include "
            "inspected surrounding context for completing sentences and reactions. "
            "do not bridge source gaps. Return title, story, hook, payoff, segments with start/end, "
            "hook_evidence and payoff_evidence. For speech evidence give kind='speech', time and a SHORT "
            "EXACT quote copied from the supplied timestamped words. For a visual change give kind='visual', "
            "before_time, time and detail of the change in the supplied timestamped images. Both pieces "
            "of evidence must be distinct and chronological. ALL times are seconds from GAME start, "
            "never VOD/source time. Return segments as [{\"start\":10,\"end\":25}], using actual game-relative "
            "times, and put hook_evidence and payoff_evidence at the top level. "
            f"Rough candidate: {json.dumps(evidence, ensure_ascii=False)}. "
            f"Words: {json.dumps(words, ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames], model=self.editorial_model, detail="high",
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
