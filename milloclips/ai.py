from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from .io import write_json


STATES = ["OTHER", "QUEUE", "CHAMP_SELECT", "LOADING", "IN_GAME", "POST_GAME"]
BEAT_TYPES = ["SETUP", "COMBAT", "REACTION", "STRATEGY", "FARMING", "TRAVEL", "SHOPPING", "DEAD", "RESULT"]
THUMBNAIL_HOOKS = {"high-stakes/amazing moment", "unusual pick/role/challenge", "bold claim/strategy", "extremely good performance"}
THUMBNAIL_FILLER = {"a", "al", "como", "con", "de", "el", "en", "es", "esto", "fucking", "fck", "jugué", "jugue", "la", "locura", "me", "mi", "mí", "mira", "momento", "muy", "para", "pick", "que", "rarísimo", "si", "un", "una", "va", "volando", "yo"}
THUMBNAIL_ACTOR = {"jugué", "jugue", "jugando", "me", "mi", "mí", "mío", "mio", "yo"}


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-záéíóúüñ0-9]+", value.casefold()))


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

    def _json(self, prompt: str, images: list[Path] | None = None, *, model: str | None = None, detail: str = "low") -> Any:
        model = model or self.model
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images or []:
            encoded = base64.b64encode(image.read_bytes()).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": detail},
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
        evidence = [
            {
                "game_id": game["game_id"],
                "duration_seconds": game["duration"],
                "beats": [
                    {key: beat.get(key) for key in ("type", "importance", "keep", "text", "reason")}
                    for beat in sorted(
                        game["beats"], key=lambda beat: (bool(beat.get("keep")), beat.get("importance", 1)), reverse=True
                    )[:30]
                ],
            }
            for game in games
        ]
        parsed = self._json(
            "Rank these League games for a coherent 9-12 minute Spanish YouTube edit. "
            "Score each from 0 to 100 for story potential, humor/emotion, standout moments, "
            "and editability. Prefer a clear arc and varied meaningful beats; penalize routine "
            "or incoherent footage. Judge only supplied evidence. Return JSON only: "
            '{"games":[{"game_id":"game-001","score":85,"reason":"specific evidence-based reason"}]}. '
            "Include every game exactly once. Evidence: " + json.dumps(evidence, ensure_ascii=False),
            model=self.editorial_model,
        )
        by_id = {game["game_id"] for game in games}
        ranked = {}
        for item in parsed.get("games", []):
            if not isinstance(item, dict) or item.get("game_id") not in by_id:
                continue
            try:
                score = max(0, min(100, float(item["score"])))
            except (KeyError, TypeError, ValueError):
                continue
            ranked[item["game_id"]] = {
                "game_id": item["game_id"], "score": score,
                "reason": str(item.get("reason", ""))[:500],
            }
        if len(ranked) != len(games):
            raise ValueError("Editorial game ranking did not return a valid score for every game")
        return sorted(ranked.values(), key=lambda item: item["score"], reverse=True)

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
            "totaling about 8:40-10:50 before reusable channel cards are added. Every scene must contain a concrete "
            "premise, setup, action, outcome, joke, reversal, or reaction; never add footage merely to cover equal "
            "amounts of early/mid/late game. Exclude safe recalls, walking back to lane, routine farming, shopping, "
            "respawn waits, repeated explanations, and action whose result is not included. Preserve short setup "
            "before each action and its immediate outcome/reaction afterward. Include the pick/build premise, the "
            "strongest varied moments, escalating stakes, and the actual final fight/result. Do not start or end a "
            "scene mid-sentence. Scene ranges use inclusive adjacent beat IDs and should usually last 20-90 seconds. "
            "Mark the actual story premise and ending required=true; optional scenes must be removable without "
            "orphaning a setup or payoff. Select pregame speech only if relevant to this story; do not force it. "
            "Choose one later high-impact beat for a 5-20 second cold-open teaser; it will also remain in its normal "
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
            answer = {"segments": [
                {**part, "action_start": part["start"] + min(2, (part["end"] - part["start"]) / 4),
                 "outcome_time": part["end"] - 1, "reaction_end": part["end"] - 0.5,
                 "downtime_start": None, "downtime_kind": "none", "event_kind": "combat",
                 "start_reason": "Synthetic pre-action setup", "end_reason": "Synthetic completed reaction",
                 "evidence": {}} for part in parts
            ], "needs_earlier_context": False, "needs_later_context": False}
            if cold_open:
                last = answer["segments"][-1]
                answer.update({"cold_open_start": max(last["start"], last["end"] - 10), "cold_open_end": last["end"]})
            return answer
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
            "omit the channel, shopping and walk back. For Arena cut after round outcome/reaction, before routine "
            "between-round shopping/waiting. A recall can be cancelled: do not discard a meaningful renewed fight. "
            "Keep useful speech during recall/travel until its complete sentence/joke finishes, then cut. "
            "Speech-only banter/setup is valid: event_kind='speech', action_start means premise-line start and "
            "outcome_time means punchline/explanation. Never require a visual fight for a conversation. "
            "Each segment needs start/end, action_start, outcome_time, reaction_end, downtime_start (null if none), "
            "downtime_kind ('recall','shopping','travel','waiting','none'), event_kind ('combat' or 'speech'), "
            "start_reason/end_reason, and evidence for action_start/outcome_time/reaction_end and any downtime_start. "
            "Each evidence object has kind='visual', time equal to a SUPPLIED image timestamp, detail of what is visible; "
            "or kind='speech', time and an EXACT quote from timestamped words. Anchor times must equal evidence times. "
            "reaction_end denotes the last necessary reaction anchor; retain its whole spoken phrase. "
            "In precise pass start/end must stay in their corresponding boundary_windows AND allowed_region. "
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
        )

    def thumbnail_concepts(self, beats: list[dict], story: str, images: list[Path]) -> list[dict]:
        if self.mock:
            choices = [("ME PASÓ ESTO", "action", "shocked"),
                       ("JUGUÉ ESTO", "identity", "hold-on"),
                       ("ME VOLVIÓ LOCO", "action", "hyped")]
            return [
                {"title": headline.title(), "headline": headline, "hook": "high-stakes/amazing moment",
                 "template": template, "expression": expression, "beat_id": beats[index % len(beats)]["id"]}
                for index, (headline, template, expression) in enumerate(choices)
            ] if beats else []
        evidence = [{key: beat.get(key) for key in ("id", "type", "importance", "text", "reason")} for beat in beats]
        prompt = """
Create exactly three distinct Spanish YouTube title/thumbnail concepts for this FINAL League edit.
For hook, use EXACTLY one of these strings: "high-stakes/amazing moment",
"unusual pick/role/challenge", "bold claim/strategy", "extremely good performance".
A strategy can claim it works only when the evidence proves its payoff. Do not turn a hypothetical
("podría", "casi", "si") into an achieved result. Headlines are 2-4 Spanish words, all-caps-friendly,
punchy editorial paraphrases. Every specific noun or claim in both title and headline must appear
verbatim in the cited evidence; only generic words such as MIRA, ESTO, MOMENTO, QUÉ, and LOCURA may
be added. Both title and headline must relate the event to Elmiillor in first person (JUGUÉ, ME, MI,
YO); never use a bare enemy or teammate champion as the subject. Titles complement rather than repeat
the headline. Use template action, identity, or strategy.
Expressions: shocked for stakes, hold-on for unusual/challenge, hyped for strategy/performance,
smiling for satisfying payoff. Cite a supplied beat_id. Return JSON only:
Each concept must include evidence: an exact short phrase copied from its cited beat's spoken text.
If a supplied beat cannot support a concise truthful concept, omit it rather than guessing.
{"concepts":[{"title":"...","headline":"...","hook":"...","template":"action","expression":"shocked","beat_id":1,"evidence":"exact copied phrase","champion":"Fiddlesticks"}]}
        Story: """ + story + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False)
        parsed = self._json(prompt, images, model=getattr(self, "editorial_model", getattr(self, "model", "gpt-5-nano")))
        write_json(self.vod_dir / "thumbnail-concepts.raw.json", parsed)
        valid_ids = {beat["id"] for beat in beats}
        output = []
        by_id = {beat["id"]: beat for beat in beats}
        for concept in parsed.get("concepts", []):
            if concept.get("beat_id") not in valid_ids:
                continue
            headline = str(concept.get("headline", "")).strip()[:40]
            if not headline or len(headline.split()) > 4:
                continue
            evidence = str(concept.get("evidence", "")).strip()[:160]
            source = _words(str(by_id[concept["beat_id"]].get("text", "")))
            cited = _words(evidence)
            title = str(concept.get("title", headline)).strip()[:100]
            claims = _words(f"{title} {headline}") - THUMBNAIL_FILLER
            if not evidence or not cited & source or any(not any(word.startswith(term) or term.startswith(word) for word in source) for term in claims):
                continue
            if concept.get("hook") not in THUMBNAIL_HOOKS or concept.get("template") not in {"action", "identity", "strategy"}:
                continue
            if concept.get("expression") not in {"shocked", "hold-on", "hyped", "smiling"}:
                continue
            if {"podria", "podría", "casi", "si"} & cited and concept["hook"] in {"high-stakes/amazing moment", "extremely good performance"}:
                continue
            if not _words(f"{title} {headline}") & THUMBNAIL_ACTOR:
                continue
            output.append({
                "title": title,
                "headline": headline.upper(),
                "hook": str(concept.get("hook", "")).strip()[:120],
                "template": str(concept.get("template", "action")).strip(),
                "expression": str(concept.get("expression", "shocked")).strip(),
                "beat_id": concept["beat_id"],
                "evidence": evidence,
                "champion": str(concept.get("champion", "")).strip() if str(concept.get("champion", "")).isalnum() else "",
            })
        return output[:3]

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
                "a kill is NOT an event without an exceptional visible or spoken outcome. A strong rant is valid "
                "only when it has an immediately quotable hook and escalates to a sharper punchline. Return zero if none. "
                "Beat labels and importance may be wrong. Give the ID of the beat containing the payoff. "
                "Use one continuous scene for a play, or 2-4 chronological scenes only for a real callback. "
                "Each scene uses inclusive beat IDs; target 20-45 seconds and never exceed 60 after trimming. "
                "Return {\"candidates\":[{\"title\":\"...\",\"proposed_payoff\":\"specific event\","
                "\"payoff_beat_id\":2,\"scenes\":[{\"start_beat_id\":1,\"end_beat_id\":2}]}]}. Beats:\n"
                + json.dumps(window, ensure_ascii=False), model=self.editorial_model,
            )
            if isinstance(parsed.get("candidates"), list):
                candidates.extend(parsed["candidates"][:3])
        # A separate wide view may find a callback that spans several minutes.
        parsed = self._json(
            "Return JSON only. Find at most TWO surprising or hilarious callbacks across this League game. "
            "An actual ending must overturn or deliver on an earlier premise; no generic game recaps, "
            "plans, or unrelated highlights. Zero is fine. Use 2-4 short chronological scenes, "
            "target 20-45 seconds and never exceed 60 after trimming, and identify the exact payoff beat. "
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
            "Return JSON only. Coarsely scout this part of a League game using ordered still images. "
            "These still images are in time order, not continuous video. Find at most ONE genuinely "
            "exceptional VISUAL Short where the streamer's champion visibly makes a decisive play, or "
            "an unmistakably surprising outcome occurs on screen. Use before/after frames as evidence. "
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
            "The total selected footage should target 20-45 seconds and never exceed 60 after trimming. Give payoff_beat_id "
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
            "Return JSON only. You are checking a Short for a stranger who will swipe in two seconds. "
            "Images are timestamped stills, NOT continuous video. Speech is timestamped separately. "
            "Reject ordinary gameplay commentary, recalls, plans without a surprising result, or a moment "
            "that needs a title to become interesting. Do NOT invent wins, kills or abilities from dialogue. "
            "If the hook cannot be HEARD or SEEN within the first 2 seconds of the actual cut, reject. "
            "If the payoff is not evidenced near the end of the cut, reject. A spoken self-correction can "
            "be a payoff. Return {\"reject\":true,\"reason\":\"one sentence\"} if either fails. No scores. Otherwise return JSON "
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
            "A strong rant may pass only with a quotable opening and clear escalation or punchline. "
            "Keep 1 scene for a self-contained play, or 2-4 for an arc; never pad to reach a scene count. "
            "Target 20-45 seconds and never exceed 60. Long time jumps must be self-evident from repeated "
            "wording, champion identity, or visible context because no editorial time labels will be added. "
            f"Proposal: {json.dumps(proposal, ensure_ascii=False)}. "
            f"Beats: {json.dumps(beats, ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames],
            model=self.editorial_model, detail="high",
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
            "An expressive facecam or vocal reaction is a bonus, not a requirement. When quality is close, "
            "prefer a varied pair such as one personality/rant clip and one gameplay/reversal clip. "
            "Choose ZERO if none are genuinely entertaining; do not fill a quota. "
            "Return {\"selected_ids\":[0,1],\"reason\":\"why these beat the alternatives\"}. "
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
            "A strong rant may pass when its opening is immediately quotable and its ending clearly escalates "
            "or lands a sharper punchline; repeated blame without a turn fails. Return {\"reject\":true} if this "
            "story would not stop a League viewer scrolling. Otherwise choose the strongest self-contained "
            "20-45 second edit of the SAME story, with an absolute range of 8-60 seconds. For a multi-scene candidate, "
            "return the same number of chronological segments, each trimmed inside its corresponding rough segment; "
            "do not bridge source gaps. Return title, story, hook, payoff, segments with start/end, "
            "hook_evidence and payoff_evidence. For speech evidence give kind='speech', time and a SHORT "
            "EXACT quote copied from the supplied timestamped words. For a visual change give kind='visual', "
            "before_time, time and detail of the change in the supplied timestamped images. Both pieces "
            "of evidence must be distinct and chronological. Times are seconds from game start. "
            f"Rough candidate: {json.dumps(candidate, ensure_ascii=False)}. "
            f"Words: {json.dumps(words, ensure_ascii=False)}. "
            f"Images in order: {json.dumps([frame['timestamp'] for frame in frames])}",
            [Path(frame["path"]) for frame in frames], model=self.editorial_model, detail="high",
        )

    def audit_short(self, candidate: dict, words: list[dict], frames: list[dict]) -> dict:
        if self.mock:
            return {"keep": True, "class": "synthetic", "reason": "Synthetic fixture passes the quality gate"}
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
            "frame at the cited timestamp, not inferred from a later push. Also keep a strong rant only when "
            "it opens with an immediately quotable line and visibly or vocally escalates to a sharper punchline; "
            "repeating the same complaint is not escalation. Ignore repeated single-letter transcript garbage. "
            "Classify kept clips as reversal, self-own, escalation, visual-highlight, or rant. Otherwise reject. "
            "Return {\"keep\":true/false,\"class\":\"...\",\"reason\":\"one sentence\"}. "
            f"Candidate: {json.dumps(candidate, ensure_ascii=False)}. "
            f"Words in cut: {json.dumps(words, ensure_ascii=False)}. "
            f"Frames in order: {json.dumps([frame['timestamp'] for frame in frames])}",
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
