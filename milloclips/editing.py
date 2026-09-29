from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from PIL import ImageFont

from .ai import AI
from .fonts import ASS_NAME, FILE
from .io import read_json, write_json
from .media import extract_audio, render_segments, render_vertical, sample_frames


def _cache_matches(data: dict, **expected: object) -> bool:
    return all(data.get(key) == value for key, value in expected.items())


def _selected_beats(vod_dir: Path, cache_key: dict) -> list[dict]:
    data = read_json(vod_dir / "beats.json")
    if not _cache_matches(data, **cache_key):
        raise SystemExit("beats.json does not match the selected game; run analyze first")
    return data["beats"]


def games(vod_dir: Path) -> list[dict]:
    reviewed = vod_dir / "games.reviewed.json"
    return read_json(reviewed if reviewed.exists() else vod_dir / "games.json")["games"]


def select_game(vod_dir: Path, game_id: str) -> dict:
    game = next((item for item in games(vod_dir) if item["id"] == game_id), None)
    if game is None:
        raise SystemExit(f"Unknown game id: {game_id}")
    write_json(vod_dir / "selection.json", {"game_id": game_id})
    return game


def selected_game(vod_dir: Path) -> dict:
    selection = read_json(vod_dir / "selection.json")
    return next(item for item in games(vod_dir) if item["id"] == selection["game_id"])


def short_candidates(vod_dir: Path) -> list[dict]:
    reviewed = vod_dir / "short-candidates.reviewed.json"
    raw = read_json(vod_dir / "short-candidates.json")
    if reviewed.exists():
        correction = read_json(reviewed)
        if correction.get("version") == raw.get("version") and correction.get("game_id") == raw.get("game_id"):
            return correction["candidates"]
    return raw["candidates"]


def transcript_words(vod_dir: Path) -> list[dict]:
    words = read_json(vod_dir / "transcript.json").get("words", [])
    review = vod_dir / "transcript.reviewed.json"
    reviewed = read_json(review) if review.exists() else {}
    ranges = reviewed.get("exclude_ranges", [])
    replacements = reviewed.get("replacements", [])
    words = [
        {
            **word,
            "word": next(
                (
                    item["text"]
                    for item in replacements
                    if float(item["start"]) <= float(word.get("start", 0))
                    and float(item["end"]) >= float(word.get("end", 0))
                ),
                word.get("word", ""),
            ),
        }
        for word in words
        if not any(float(item["start"]) < float(word.get("end", 0)) and float(item["end"]) > float(word.get("start", 0)) for item in ranges)
    ]
    repair_path = vod_dir / "short-transcript.json"
    selection = vod_dir / "selection.json"
    if repair_path.exists() and selection.exists():
        repair = read_json(repair_path)
        if repair.get("game_id") == read_json(selection).get("game_id"):
            correction = vod_dir / "short-transcript.reviewed.json"
            excluded = read_json(correction).get("exclude_ranges", []) if correction.exists() else []
            for item in repair.get("repairs", []):
                start, end = item["relative_start"], item["relative_end"]
                words = [word for word in words if float(word["start"]) >= end or float(word["end"]) <= start]
                words.extend(
                    {**word, "start": start + float(word["start"]), "end": start + float(word["end"])}
                    for word in item.get("words", [])
                    if not any(float(part["start"]) < start + float(word["end"]) and float(part["end"]) > start + float(word["start"]) for part in excluded)
                )
    return sorted(words, key=lambda word: float(word["start"]))


def repair_short_audio(vod_dir: Path, ai: AI, candidate_index: int = 0) -> Path:
    candidate = short_candidates(vod_dir)[candidate_index]
    game = selected_game(vod_dir)
    path = vod_dir / "short-transcript.json"
    cached = read_json(path) if path.exists() else {}
    repairs = cached.get("repairs", []) if cached.get("game_id") == game["id"] and cached.get("gameplay_start") == game["gameplay_start"] else []
    words = transcript_words(vod_dir)
    missing = []
    for segment in candidate.get("segments", [candidate]):
        start, end = segment["relative_start"], segment["relative_end"]
        if any(float(word["start"]) < end and float(word["end"]) > start for word in words):
            continue
        if missing and abs(missing[-1][1] - start) < 0.01:
            missing[-1] = (missing[-1][0], end)
        else:
            missing.append((start, end))
    source = Path(read_json(vod_dir / "manifest.json")["source"])
    for start, end in missing:
        if any(abs(item["relative_start"] - start) < 0.01 and abs(item["relative_end"] - end) < 0.01 for item in repairs):
            continue
        audio = vod_dir / f"short-audio-{start:.2f}-{end:.2f}.mp3"
        if not audio.exists():
            extract_audio(source, audio, game["gameplay_start"] + start, game["gameplay_start"] + end)
        transcript = ai.transcribe(audio, end - start)
        repairs.append({"relative_start": start, "relative_end": end, "text": transcript.get("text", ""), "words": transcript.get("words", [])})
        write_json(path, {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "repairs": repairs})
    return path


def _beat_intervals(
    duration: float,
    words: list[dict],
    target: float = 15,
    minimum: float = 8,
    maximum: float = 25,
) -> list[tuple[float, float]]:
    pauses = [
        (float(left.get("end", 0)) + float(right.get("start", 0))) / 2
        for left, right in zip(words, words[1:])
        if float(right.get("start", 0)) - float(left.get("end", 0)) >= 0.75
    ]
    intervals: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > maximum:
        candidates = [pause for pause in pauses if start + minimum <= pause <= start + maximum]
        end = min(candidates, key=lambda pause: abs(pause - start - target)) if candidates else start + target
        intervals.append((start, end))
        start = end
    if intervals and duration - start < minimum:
        previous_start = intervals[-1][0]
        if duration - previous_start <= maximum:
            intervals[-1] = (previous_start, duration)
        else:
            final_start = duration - minimum
            intervals[-1] = (previous_start, final_start)
            intervals.append((final_start, duration))
    else:
        intervals.append((start, duration))
    return intervals


def analyze(vod_dir: Path, ai: AI, force: bool = False, beat_seconds: float = 15) -> list[dict]:
    output = vod_dir / "beats.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    cache_key = {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end}
    cached = read_json(output) if output.exists() else {}
    if output.exists() and not force:
        if _cache_matches(cached, **cache_key, target_beat_seconds=beat_seconds):
            return cached["beats"]
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    game_duration = editorial_end - game["gameplay_start"]
    champ_select_start = game.get("champ_select_start")
    pregame_start = (
        max(float(champ_select_start), game["gameplay_start"] - 180)
        if champ_select_start is not None and champ_select_start < game["gameplay_start"]
        else game["gameplay_start"]
    )
    pregame_duration = game["gameplay_start"] - pregame_start
    old_beats = cached.get("beats", []) if cached.get("game_id") == game["id"] else []
    old_game_end = max((float(beat["end"]) for beat in old_beats if beat.get("phase") != "CHAMP_SELECT"), default=-1)
    old_pregame_start = min((float(beat["start"]) for beat in old_beats if beat.get("phase") == "CHAMP_SELECT"), default=0)
    legacy_main_cache = abs(old_game_end - game_duration) < 0.01
    legacy_pregame_cache = legacy_main_cache and abs(old_pregame_start + pregame_duration) < 0.01

    transcript_path = vod_dir / "transcript.json"
    transcript = read_json(transcript_path) if transcript_path.exists() else {}
    transcript_key = {"game_id": game["id"], "audio_start": game["gameplay_start"], "audio_end": editorial_end}
    if _cache_matches(transcript, **transcript_key) or ("game_id" not in transcript and legacy_main_cache):
        if "game_id" not in transcript:
            transcript.update(transcript_key)
            write_json(transcript_path, transcript)
    else:
        audio = vod_dir / "selected-game.mp3"
        extract_audio(source, audio, game["gameplay_start"], editorial_end)
        transcript = ai.transcribe(audio, game_duration)
        transcript.update(transcript_key)
        write_json(transcript_path, transcript)

    frame_interval = beat_seconds / 3
    frames = sample_frames(
        source,
        vod_dir / "frames" / "selected-game",
        frame_interval,
        start=pregame_start,
        end=editorial_end,
    )
    words = transcript_words(vod_dir)
    beats = []
    if pregame_duration:
        pregame_path = vod_dir / "pregame-transcript.json"
        pregame = read_json(pregame_path) if pregame_path.exists() else {}
        pregame_key = {"game_id": game["id"], "audio_start": pregame_start, "audio_end": game["gameplay_start"]}
        if _cache_matches(pregame, **pregame_key) or ("game_id" not in pregame and legacy_pregame_cache):
            if "game_id" not in pregame:
                pregame.update(pregame_key)
                write_json(pregame_path, pregame)
        else:
            audio = vod_dir / "selected-pregame.mp3"
            extract_audio(source, audio, pregame_start, game["gameplay_start"])
            pregame = ai.transcribe(audio, pregame_duration)
            pregame.update(pregame_key)
            write_json(pregame_path, pregame)
        pregame_words = [
            {**word, "start": float(word.get("start", 0)) - pregame_duration, "end": float(word.get("end", 0)) - pregame_duration}
            for word in pregame.get("words", [])
        ]
        pregame_intervals = _beat_intervals(pregame_duration, pregame.get("words", []), target=beat_seconds)
        for index, (local_start, local_end) in enumerate(pregame_intervals):
            start = local_start - pregame_duration
            end = local_end - pregame_duration
            text = " ".join(
                str(word.get("word", "")).strip()
                for word in pregame_words
                if float(word.get("start", 0)) < end and float(word.get("end", 0)) >= start
            ).strip()
            beats.append({"id": index - len(pregame_intervals), "start": start, "end": end, "text": text, "phase": "CHAMP_SELECT"})
    for beat_id, (start, end) in enumerate(_beat_intervals(game_duration, words, target=beat_seconds)):
        text = " ".join(
            str(word.get("word", "")).strip()
            for word in words
            if float(word.get("start", 0)) < end and float(word.get("end", 0)) >= start
        ).strip()
        beats.append({"id": beat_id, "start": start, "end": end, "text": text, "phase": "IN_GAME"})

    classified: list[dict] = []
    for offset in range(0, len(beats), 4):
        batch = beats[offset : offset + 4]
        start = batch[0]["start"]
        end = batch[-1]["end"]
        batch_frames = [
            Path(frame["path"])
            for frame in frames
            if game["gameplay_start"] + start <= frame["timestamp"] < game["gameplay_start"] + end
        ][: len(batch) * 3]
        classified.extend(ai.classify_beats(batch, batch_frames))

    gameplay_frames = [frame for frame in frames if frame["timestamp"] >= game["gameplay_start"]]
    layout = ai.detect_layout(Path(gameplay_frames[0]["path"])) if gameplay_frames else {"facecam": [0.02, 0.03, 0.28, 0.30], "confidence": 0}
    write_json(vod_dir / "layout.json", layout)
    write_json(output, {**cache_key, "target_beat_seconds": beat_seconds, "beats": classified})
    return classified


def _enforce_duration(
    beats: list[dict],
    selected_ids: list[int],
    minimum: float = 540,
    maximum: float = 720,
    max_run: float = 120,
    min_phase: float = 180,
    required_ids: set[int] | None = None,
) -> list[int]:
    valid = {beat["id"] for beat in beats}
    selected = {int(value) for value in selected_ids if isinstance(value, int) and value in valid}
    required = (required_ids or set()) & valid
    selected.update(required)
    by_id = {beat["id"]: beat for beat in beats}

    def total() -> float:
        return sum(beat["end"] - beat["start"] for beat in beats if beat["id"] in selected)

    def long_run() -> list[dict]:
        runs: list[list[dict]] = []
        for beat in beats:
            if beat["id"] not in selected:
                continue
            if runs and runs[-1][-1]["end"] == beat["start"]:
                runs[-1].append(beat)
            else:
                runs.append([beat])
        return max(runs, key=lambda run: sum(item["end"] - item["start"] for item in run), default=[])

    game_end = max((beat["end"] for beat in beats if beat["start"] >= 0), default=0)

    def phase(beat: dict) -> int | None:
        if beat["start"] < 0 or game_end == 0:
            return None
        return min(2, int(beat["start"] / game_end * 3))

    def phase_total(index: int) -> float:
        return sum(beat["end"] - beat["start"] for beat in beats if beat["id"] in selected and phase(beat) == index)

    while True:
        run = long_run()
        if sum(beat["end"] - beat["start"] for beat in run) <= max_run:
            break
        middle = (run[0]["start"] + run[-1]["end"]) / 2
        removable_run = [beat for beat in run if beat["id"] not in required]
        if not removable_run:
            break
        remove = min(
            removable_run,
            key=lambda beat: (beat["importance"], beat["keep"], abs((beat["start"] + beat["end"]) / 2 - middle)),
        )
        selected.remove(remove["id"])

    for index in range(3):
        additions = sorted(
            (beat for beat in beats if beat["id"] not in selected and phase(beat) == index),
            key=lambda beat: (beat["importance"], beat["keep"]),
            reverse=True,
        )
        for beat in additions:
            if phase_total(index) >= min_phase:
                break
            for beat_id in (beat["id"] - 1, beat["id"], beat["id"] + 1):
                item = by_id.get(beat_id)
                if item is None or item["id"] in selected or phase(item) != index:
                    continue
                if beat_id != beat["id"] and not item.get("text") and item.get("type") in {"FARMING", "TRAVEL", "SHOPPING", "DEAD"}:
                    continue
                selected.add(item["id"])
                if sum(part["end"] - part["start"] for part in long_run()) > max_run:
                    selected.remove(item["id"])

    removable = sorted(
        (beat for beat in beats if beat["id"] in selected and beat["id"] not in required),
        key=lambda beat: (
            beat["importance"],
            beat["keep"],
            beat["id"] - 1 in selected and beat["id"] + 1 in selected,
        ),
    )
    for beat in removable:
        if total() <= maximum:
            break
        beat_phase = phase(beat)
        if beat_phase is not None and phase_total(beat_phase) - (beat["end"] - beat["start"]) < min_phase:
            continue
        selected.remove(beat["id"])

    additions = sorted(
        (beat for beat in beats if beat["id"] not in selected),
        key=lambda beat: (beat["importance"], beat["keep"]),
        reverse=True,
    )
    for beat in additions:
        if total() >= minimum:
            break
        selected.add(beat["id"])
        if sum(item["end"] - item["start"] for item in long_run()) > max_run:
            selected.remove(beat["id"])
    return sorted(selected)


def plan_long(vod_dir: Path, ai: AI, force: bool = False) -> dict:
    output = vod_dir / "long-plan.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    cache_key = {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end}
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key):
            return cached
    beats = _selected_beats(vod_dir, cache_key)
    model_plan = ai.plan_long(beats)
    required = {beats[-1]["id"]} if game.get("result_end") is not None and beats else set()
    selected = _enforce_duration(beats, model_plan.get("selected_ids", []), required_ids=required)
    selected_beats = [beat for beat in beats if beat["id"] in selected]
    segments = []
    for beat in selected_beats:
        source_start = game["gameplay_start"] + beat["start"]
        source_end = game["gameplay_start"] + beat["end"]
        if segments and abs(segments[-1]["source_end"] - source_start) < 0.01:
            segments[-1]["source_end"] = source_end
            segments[-1]["beat_ids"].append(beat["id"])
        else:
            segments.append({"source_start": source_start, "source_end": source_end, "beat_ids": [beat["id"]]})
    plan = {
        **cache_key,
        "story": model_plan.get("story", ""),
        "selected_ids_raw": model_plan.get("selected_ids", []),
        "selected_ids": selected,
        "duration": sum(segment["source_end"] - segment["source_start"] for segment in segments),
        "segments": segments,
    }
    write_json(output, plan)
    return plan


def render_long(vod_dir: Path) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    plan = read_json(vod_dir / "long-plan.json")
    output = vod_dir / "renders" / "long-rough.mp4"
    render_segments(Path(manifest["source"]), plan["segments"], output)
    return output


def _spoken_span(quote: str, words: list[dict], start: float, end: float) -> tuple[float, float] | None:
    def tokens(text: str) -> list[str]:
        plain = "".join(letter for letter in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(letter))
        return re.findall(r"\w+", plain)

    phrase = tokens(quote)
    if len(phrase) < 2:
        return None
    spoken = [(token, float(word["start"]), float(word["end"])) for word in words
              if start <= float(word.get("start", -1)) < end
              for token in tokens(str(word.get("word", "")))]
    for index in range(len(spoken) - len(phrase) + 1):
        if [part[0] for part in spoken[index : index + len(phrase)]] == phrase:
            return spoken[index][1], max(spoken[index + len(phrase) - 1][2], spoken[index][1] + 0.1)
    return None


def _short_evidence(evidence: dict, segment: dict, words: list[dict], frames: list[dict], *, hook: bool) -> bool:
    if not isinstance(evidence, dict) or type(evidence.get("time")) not in (int, float):
        return False
    time = evidence["time"]
    start, end = segment["relative_start"], segment["relative_end"]
    if not math.isfinite(time) or not start <= time < end:
        return False
    if evidence.get("kind") == "visual":
        before = evidence.get("before_time")
        return ((start <= time < min(end, start + 2) if hook else max(start, end - 8) <= time < end)
                and type(before) in (int, float) and math.isfinite(before) and start <= before < time
                and len(str(evidence.get("detail", "")).strip()) >= 12
                and all(any(abs(frame["timestamp"] - moment) <= 1 for frame in frames) for moment in (before, time)))
    if evidence.get("kind") != "speech":
        return False
    span = _spoken_span(str(evidence.get("quote", "")), words, start, end)
    return span is not None and abs(span[0] - time) <= 0.5 and (span[0] < start + 2 if hook else span[1] >= end - 8)


def _resolve_evidence(evidence: object, words: list[dict], frames: list[dict], start: float, end: float, *, prefer_last: bool = False) -> tuple[dict, float] | None:
    matches = []
    for item in evidence if isinstance(evidence, list) else [evidence]:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "speech":
            span = _spoken_span(str(item.get("quote", "")), words, start, end)
            if span:
                matches.append(({**item, "time": span[0]}, span[1]))
        if item.get("kind") == "visual" and type(item.get("time")) in (int, float):
            time, before = item["time"], item.get("before_time")
            if (math.isfinite(time) and type(before) in (int, float) and math.isfinite(before)
                    and start <= before < time < end and len(str(item.get("detail", "")).strip()) >= 12
                    and all(any(abs(frame["timestamp"] - moment) <= 1 for frame in frames) for moment in (before, time))):
                matches.append((item, time))
    return matches[-1 if prefer_last else 0] if matches else None


def _retime_short(candidate: dict, answer: dict, words: list[dict], frames: list[dict], game_start: float, region: tuple[float, float]) -> dict | None:
    if not isinstance(answer, dict) or answer.get("reject"):
        return None
    if not all(isinstance(answer.get(key), str) and answer[key].strip() for key in ("title", "story", "hook", "payoff")):
        return None
    hook = _resolve_evidence(answer.get("hook_evidence"), words, frames, *region)
    payoff = _resolve_evidence(answer.get("payoff_evidence"), words, frames, *region, prefer_last=True)
    if not hook or not payoff:
        return None
    if answer.get("event_evidence"):
        event = _resolve_evidence(answer["event_evidence"], words, frames, *region)
        if not event or not hook[0]["time"] < event[0]["time"] < payoff[0]["time"]:
            return None
    start = hook[0]["before_time"] if hook[0]["kind"] == "visual" else max(region[0], hook[0]["time"] - 0.25)
    end = payoff[1] + 1
    if payoff[0]["kind"] == "speech":
        previous_end = payoff[1]
        for word in words:
            word_start = float(word.get("start", -1))
            if word_start < payoff[1] or word_start > payoff[1] + 5:
                continue
            if word_start - previous_end >= 0.75:
                end = previous_end + 0.35
                break
            previous_end = max(previous_end, float(word.get("end", word_start)))
        else:
            if previous_end > payoff[1]:
                end = previous_end + 0.35
    end = min(region[1], end)
    if not region[0] <= start < end <= region[1] or not 8 <= end - start <= 90:
        return None
    segment = {"relative_start": start, "relative_end": end, "source_start": game_start + start, "source_end": game_start + end}
    if not _short_evidence(hook[0], segment, words, frames, hook=True) or not _short_evidence(payoff[0], segment, words, frames, hook=False):
        return None
    return {
        **candidate, "segments": [segment], **segment, "duration": end - start,
        "title": answer["title"][:100], "reason": answer["story"][:240],
        "hook": answer["hook"][:240], "payoff": answer["payoff"][:240],
        "hook_evidence": hook[0], "payoff_evidence": payoff[0],
        **({"event_evidence": event[0]} if answer.get("event_evidence") else {}),
    }


def _excluded(start: float, end: float, exclusions: list[dict]) -> bool:
    return any(start < item["end"] and end > item["start"] for item in exclusions)


def find_shorts(vod_dir: Path, ai: AI, force: bool = False) -> list[dict]:
    output = vod_dir / "short-candidates.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    reviewed_exclusions = vod_dir / "short-exclusions.reviewed.json"
    corrections = read_json(reviewed_exclusions) if reviewed_exclusions.exists() else {}
    if corrections and corrections.get("game_id") != game["id"]:
        raise ValueError("Short exclusions do not match the selected game")
    exclusions = corrections.get("ranges", [])
    if not isinstance(exclusions, list) or any(
        not isinstance(item, dict) or type(item.get("start")) not in (int, float)
        or type(item.get("end")) not in (int, float) or not math.isfinite(item["start"])
        or not math.isfinite(item["end"]) or not 0 <= item["start"] < item["end"] <= editorial_end - game["gameplay_start"]
        for item in exclusions
    ):
        raise ValueError("Invalid short exclusion ranges")
    cache_key = {"version": 7, "game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end,
                 "exclusions": exclusions}
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key, evidence_version=2, cuts_version=4, output_version=9, callback_version=1):
            return cached["candidates"]
    beats = [beat for beat in _selected_beats(vod_dir, {key: value for key, value in cache_key.items() if key not in ("version", "exclusions")}) if beat.get("phase") != "CHAMP_SELECT"]
    by_id = {beat["id"]: index for index, beat in enumerate(beats)}
    proposals_path = vod_dir / "short-proposals.json"
    cached_proposals = read_json(proposals_path) if proposals_path.exists() else {}
    if not force and _cache_matches(cached_proposals, **cache_key):
        proposals = cached_proposals["candidates"]
    else:
        proposals = ai.shorts([beat for beat in beats if not _excluded(beat["start"], beat["end"], exclusions)])
        write_json(proposals_path, {**cache_key, "candidates": proposals})
    frame_dir = vod_dir / "frames" / "selected-game"
    frames = sorted(frame_dir.glob("frame_*.jpg"))
    champ_select = game.get("champ_select_start")
    pregame_start = max(float(champ_select), game["gameplay_start"] - 180) if champ_select is not None and champ_select < game["gameplay_start"] else game["gameplay_start"]
    interval = read_json(vod_dir / "beats.json").get("target_beat_seconds", 15) / 3
    stamped = [{"timestamp": pregame_start + index * interval - game["gameplay_start"], "path": str(path)} for index, path in enumerate(frames)]
    quiet_runs = []
    run = []
    for beat in [*beats, {"start": editorial_end - game["gameplay_start"], "end": editorial_end - game["gameplay_start"], "text": ""}]:
        tokens = beat.get("text", "").upper().split()
        if len(tokens) >= 4 and len(set(tokens)) == 1 and not _excluded(beat["start"], beat["end"], exclusions):
            run.append(beat)
            continue
        if run and run[-1]["end"] - run[0]["start"] >= 45:
            quiet_runs.append((max(0, run[0]["start"] - 15), run[-1]["end"]))
        run = []
    visual_path = vod_dir / "short-visual-proposals.json"
    cached_visual = read_json(visual_path) if visual_path.exists() else {}
    if not force and _cache_matches(cached_visual, **cache_key):
        visual_proposals = cached_visual["candidates"]
    else:
        visual_proposals = []
        for start, end in quiet_runs:
            nearby = [frame for frame in stamped if start <= frame["timestamp"] < end]
            for offset in range(0, len(nearby), 12):
                batch = nearby[offset : offset + 12]
                if len(batch) < 3:
                    continue
                for event in ai.visual_shorts(batch):
                    if not isinstance(event, dict) or any(type(event.get(key)) not in (int, float) or not math.isfinite(event[key]) for key in ("start", "end", "payoff")):
                        continue
                    left, right, payoff = event["start"], event["end"], event["payoff"]
                    if not (batch[0]["timestamp"] - 5 <= left < payoff < right <= batch[-1]["timestamp"] + 5) or right - left > 90 or _excluded(left, right, exclusions):
                        continue
                    scene_start = next((b["id"] for b in beats if b["end"] > left), None)
                    scene_end = next((b["id"] for b in beats if b["end"] >= right), None)
                    payoff_id = next((b["id"] for b in beats if b["end"] > payoff), None)
                    if None not in (scene_start, scene_end, payoff_id):
                        visual_proposals.append({"title": str(event.get("description", "Visual play"))[:100], "proposed_payoff": str(event.get("description", ""))[:240],
                                                 "payoff_beat_id": payoff_id, "scenes": [{"start_beat_id": scene_start, "end_beat_id": scene_end}]})
        write_json(visual_path, {**cache_key, "candidates": visual_proposals})
    callback_path = vod_dir / "short-callback-proposals.json"
    cached_callbacks = read_json(callback_path) if callback_path.exists() else {}
    if not force and _cache_matches(cached_callbacks, **cache_key, callback_version=1):
        callbacks = cached_callbacks["candidates"]
    else:
        callbacks = []
        for start, end in quiet_runs:
            nearby = [frame for frame in stamped if start <= frame["timestamp"] < end]
            earlier = [{key: beat[key] for key in ("id", "start", "end", "text")}
                       for beat in beats if beat["end"] < start and beat["importance"] >= 3
                       and beat.get("text") and not _excluded(beat["start"], beat["end"], exclusions)]
            for offset in range(0, len(nearby), 12):
                batch = nearby[offset : offset + 12]
                if len(batch) < 3:
                    continue
                late = [{"id": beat["id"], "start": beat["start"], "end": beat["end"]}
                        for beat in beats if beat["start"] < batch[-1]["timestamp"] + interval and beat["end"] > batch[0]["timestamp"]]
                callbacks.extend(ai.callback_shorts([*earlier, *late], batch)[:1])
        write_json(callback_path, {**cache_key, "callback_version": 1, "candidates": callbacks})
    normalized_callbacks = []
    for callback in callbacks:
        if not isinstance(callback, dict) or not isinstance(callback.get("scenes"), list) or not callback["scenes"]:
            continue
        scenes = [dict(scene) for scene in callback["scenes"] if isinstance(scene, dict)]
        if len(scenes) != len(callback["scenes"]):
            continue
        last = scenes[-1]
        last_start = last.get("start_beat_id")
        if type(last_start) is not int or last_start not in by_id:
            continue
        previous = beats[by_id[last_start] - 1] if by_id[last_start] else None
        if previous and previous["end"] == beats[by_id[last_start]]["start"] and previous.get("text", "").upper().split()[:4] == ["A"] * 4:
            last["start_beat_id"] = previous["id"]
        payoff_id = callback.get("payoff_beat_id")
        if type(payoff_id) is not int or payoff_id not in by_id or not last["start_beat_id"] <= payoff_id <= last.get("end_beat_id", -1):
            payoff_id = last["start_beat_id"]
        normalized_callbacks.append({**callback, "scenes": scenes, "payoff_beat_id": payoff_id, "origin": "callback"})
    proposals = [*proposals, *normalized_callbacks, *visual_proposals]
    distinct = {}
    for proposal in proposals:
        if isinstance(proposal, dict) and type(proposal.get("payoff_beat_id")) is int:
            distinct.setdefault(proposal["payoff_beat_id"], proposal)
    proposals = list(distinct.values())
    candidates = []
    source = Path(read_json(vod_dir / "manifest.json")["source"])
    words = transcript_words(vod_dir)
    verdicts_path = vod_dir / "short-verdicts.json"
    cached_verdicts = read_json(verdicts_path) if verdicts_path.exists() else {}
    verdicts = cached_verdicts.get("verdicts", {}) if _cache_matches(cached_verdicts, **cache_key) and not force else {}
    timing_path = vod_dir / "short-visual-timing.json"
    cached_timing = read_json(timing_path) if timing_path.exists() else {}
    timing = cached_timing.get("answers", {}) if not force and _cache_matches(cached_timing, **cache_key, timing_version=1) else {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        scenes = proposal.get("scenes", [])
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 7:
            continue
        ranges = []
        for scene in scenes:
            if not isinstance(scene, dict):
                break
            start_id, end_id = scene.get("start_beat_id"), scene.get("end_beat_id")
            if type(start_id) is not int or type(end_id) is not int or start_id not in by_id or end_id not in by_id or by_id[start_id] > by_id[end_id]:
                break
            start = float(beats[by_id[start_id]]["start"])
            final_index = by_id[end_id] + (int(scene is scenes[-1] and end_id == proposal.get("payoff_beat_id") and by_id[end_id] + 1 < len(beats)))
            end = float(beats[final_index]["end"])
            if not math.isfinite(start) or not math.isfinite(end) or (ranges and start < ranges[-1][0]):
                break
            if ranges and start < ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))
        payoff_id = proposal.get("payoff_beat_id")
        if not 1 <= len(ranges) <= 7 or sum(end - start for start, end in ranges) > 180 or type(payoff_id) is not int or payoff_id not in by_id or any(_excluded(start, end, exclusions) for start, end in ranges):
            continue
        payoff_beat = beats[by_id[payoff_id]]
        if not (ranges[-1][0] <= payoff_beat["start"] and payoff_beat["end"] <= ranges[-1][1]):
            continue
        verdict_key = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
        frames = []
        for index, (start, end) in enumerate(ranges):
            frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{start:.2f}-{end:.2f}-1s"
            sampled = sorted(frame_dir.glob("frame_*.jpg"))
            if not sampled:
                sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=game["gameplay_start"] + start, end=game["gameplay_start"] + end)]
            if not sampled:
                break
            if index == len(ranges) - 1:
                first = max(0, int(payoff_beat["start"] - start) - 5)
                last = min(len(sampled) - 1, int(payoff_beat["end"] - start) + 2)
                positions = {round(first + step * (last - first) / 11) for step in range(12)}
            else:
                positions = {0, len(sampled) // 2, len(sampled) - 1}
            if index == 0:
                positions.update(range(min(3, len(sampled))))
            frames.extend({"timestamp": round(start + position, 2), "path": str(sampled[position])} for position in sorted(positions))
        if proposal.get("origin") == "callback":
            frames.extend(frame for frame in stamped if ranges[-1][0] <= frame["timestamp"] < ranges[-1][1])
            frames.sort(key=lambda frame: frame["timestamp"])
        if not frames:
            continue
        if verdict_key in verdicts:
            verdict = verdicts[verdict_key]
        else:
            context = [beat for beat in beats if any(beat["start"] < end and beat["end"] > start for start, end in ranges)]
            verdict = ai.verify_short({**proposal, "scenes": [{"start": start, "end": end} for start, end in ranges]}, context, frames)
            verdicts[verdict_key] = verdict
            write_json(verdicts_path, {**cache_key, "verdicts": verdicts})
        if not isinstance(verdict, dict) or verdict.get("reject"):
            continue
        if not all(isinstance(verdict.get(key), str) and verdict[key].strip() for key in ("hook", "payoff", "story")):
            continue
        segments = verdict.get("segments")
        if not isinstance(segments, list):
            continue
        if len(segments) == 1 and len(ranges) > 1 and isinstance(segments[0], dict) and type(segments[0].get("start")) in (int, float) and type(segments[0].get("end")) in (int, float):
            if segments[0]["start"] <= ranges[0][0] + 0.01 and segments[0]["end"] >= ranges[-1][0]:
                # The model sometimes returns one interval spanning the gaps; retain its evidenced story, not the dead time.
                segments = [{"start": start, "end": end} for start, end in ranges]
        if len(segments) != len(ranges):
            continue
        trimmed = []
        for segment, (start, end) in zip(segments, ranges):
            if not isinstance(segment, dict) or type(segment.get("start")) not in (int, float) or type(segment.get("end")) not in (int, float):
                break
            left, right = segment["start"], segment["end"]
            if not math.isfinite(left) or not math.isfinite(right) or left < start - 0.01 or right > end + 0.01 or right - left < 1:
                break
            trimmed.append({"source_start": game["gameplay_start"] + left, "source_end": game["gameplay_start"] + right,
                            "relative_start": left, "relative_end": right})
        total = sum(segment["relative_end"] - segment["relative_start"] for segment in trimmed)
        if len(trimmed) != len(ranges) or not 8 <= total <= 90:
            continue
        hook = _resolve_evidence(verdict.get("hook_evidence"), words, frames, ranges[0][0], ranges[0][1])
        payoff_options = verdict.get("payoff_evidence")
        if proposal.get("origin") == "callback" and "ACE" in str(proposal.get("proposed_payoff", "")).upper() and isinstance(payoff_options, list):
            payoff_options = [item for item in payoff_options if isinstance(item, dict) and "ACE" in str(item.get("detail", "")).upper()]
        payoff = _resolve_evidence(payoff_options, words, frames, ranges[-1][0], ranges[-1][1], prefer_last=True)
        if not hook or not payoff:
            continue
        hook_evidence, payoff_evidence = hook[0], payoff[0]
        if proposal.get("origin") == "callback" and "ACE" in str(proposal.get("proposed_payoff", "")).upper() and payoff_evidence["kind"] == "visual":
            nearby = [frame for frame in frames if ranges[-1][0] <= frame["timestamp"] < ranges[-1][1]
                      and abs(frame["timestamp"] - payoff_evidence["time"]) <= 4]
            if verdict_key not in timing:
                timing[verdict_key] = ai.locate_ace(nearby)
                write_json(timing_path, {**cache_key, "timing_version": 1, "answers": timing})
            ace_time = timing[verdict_key]
            if type(ace_time) not in (int, float) or not math.isfinite(ace_time) or not any(abs(frame["timestamp"] - ace_time) < 0.01 for frame in nearby):
                continue
            before = max((frame["timestamp"] for frame in frames if ranges[-1][0] <= frame["timestamp"] < ace_time), default=None)
            if before is None:
                continue
            payoff_evidence = {"kind": "visual", "before_time": before, "time": ace_time,
                               "detail": "ACE banner appears while the named teammate is alive and the streamer is on the respawn timer"}
            payoff = (payoff_evidence, ace_time)
        trimmed[0]["relative_start"] = (max(ranges[0][0], hook_evidence["before_time"]) if hook_evidence["kind"] == "visual"
                                         else max(trimmed[0]["relative_start"], hook_evidence["time"] - 0.25))
        trimmed[0]["source_start"] = game["gameplay_start"] + trimmed[0]["relative_start"]
        trimmed[-1]["relative_end"] = min(trimmed[-1]["relative_end"], payoff[1] + 1)
        trimmed[-1]["source_end"] = game["gameplay_start"] + trimmed[-1]["relative_end"]
        total = sum(segment["relative_end"] - segment["relative_start"] for segment in trimmed)
        if not 8 <= total <= 90 or any(part["relative_end"] - part["relative_start"] < 1 for part in trimmed):
            continue
        if not _short_evidence(hook_evidence, trimmed[0], words, frames, hook=True) or not _short_evidence(payoff_evidence, trimmed[-1], words, frames, hook=False):
            continue
        if hook_evidence == payoff_evidence:
            continue
        callback = proposal in normalized_callbacks
        candidates.append(
            {
                "segments": trimmed, "source_start": trimmed[0]["source_start"], "source_end": trimmed[-1]["source_end"],
                "relative_start": trimmed[0]["relative_start"], "relative_end": trimmed[-1]["relative_end"],
                "duration": total, "title": (f"{verdict['hook']} → ACE" if callback and "ACE" in str(proposal.get("proposed_payoff", "")) else str(verdict.get("title", proposal.get("title", "Untitled"))))[:100],
                "reason": str(f"{verdict['hook']} → {payoff_evidence['detail']}" if callback and payoff_evidence["kind"] == "visual" else verdict["story"])[:240], "hook": str(verdict["hook"])[:240],
                "payoff": str(payoff_evidence["detail"] if callback and payoff_evidence["kind"] == "visual" else verdict["payoff"])[:240], "hook_evidence": hook_evidence,
                "payoff_evidence": payoff_evidence,
                "_images": [min(frames, key=lambda frame: abs(frame["timestamp"] - moment["time"]))["path"] for moment in (hook_evidence, payoff_evidence)],
            }
        )
    choices_path = vod_dir / "short-choices.json"
    cached_choices = read_json(choices_path) if choices_path.exists() else {}
    candidate_signature = json.dumps([item["segments"] for item in candidates], sort_keys=True)
    if not force and _cache_matches(cached_choices, **cache_key, candidate_signature=candidate_signature):
        chosen = cached_choices["selected_ids"]
    else:
        evidence = [{key: value for key, value in item.items() if key not in ("_images", "reason")} for item in candidates]
        chosen = ai.choose_shorts(evidence, [Path(image) for item in candidates for image in item["_images"]]) if candidates else []
        write_json(choices_path, {**cache_key, "candidate_signature": candidate_signature, "selected_ids": chosen})
    selected = []
    for index in chosen if isinstance(chosen, list) else []:
        if type(index) is not int or not 0 <= index < len(candidates) or index in selected:
            continue
        candidate = candidates[index]
        if any(
            sum(max(0, min(part["relative_end"], prior["relative_end"]) - max(part["relative_start"], prior["relative_start"]))
                for part in candidate["segments"] for prior in candidates[previous]["segments"])
            / min(candidate["duration"], candidates[previous]["duration"]) >= 0.5
            for previous in selected
        ):
            continue
        selected.append(index)
        if len(selected) == 2:
            break
    selected = [{key: value for key, value in candidates[index].items() if key != "_images"} for index in selected]
    if not getattr(ai, "mock", False):
        cuts_path = vod_dir / "short-cuts.json"
        cached_cuts = read_json(cuts_path) if cuts_path.exists() else {}
        cuts = cached_cuts.get("answers", {}) if not force and _cache_matches(cached_cuts, **cache_key, cuts_version=4) else {}
        audits_path = vod_dir / "short-audits.json"
        cached_audits = read_json(audits_path) if audits_path.exists() else {}
        audits = cached_audits.get("answers", {}) if not force and _cache_matches(cached_audits, **cache_key, cuts_version=4, audit_version=3) else {}
        refined = []
        for output_number, candidate in enumerate(selected, 1):
            if len(candidate["segments"]) != 1:
                audit_key = f"{candidate['source_start']:.2f}-{candidate['source_end']:.2f}"
                if audit_key not in audits:
                    preview = []
                    for segment in candidate["segments"]:
                        frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-audit-{segment['relative_start']:.2f}-{segment['relative_end']:.2f}"
                        sampled = sorted(frame_dir.glob("frame_*.jpg"))
                        if not sampled:
                            sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=segment["source_start"], end=segment["source_end"])]
                        count = 5 if segment is candidate["segments"][-1] else 2
                        indices = sorted({round(step * (len(sampled) - 1) / max(1, count - 1)) for step in range(count)}) if sampled else []
                        preview.extend({"timestamp": segment["relative_start"] + index, "path": str(sampled[index])} for index in indices)
                    preview.extend(frame for frame in stamped if candidate["segments"][-1]["relative_start"] <= frame["timestamp"] < candidate["segments"][-1]["relative_end"]
                                   and abs(frame["timestamp"] - candidate["payoff_evidence"]["time"]) <= 5)
                    preview.sort(key=lambda frame: frame["timestamp"])
                    cut_words = [word for word in words if any(segment["relative_start"] <= float(word["start"]) < segment["relative_end"] for segment in candidate["segments"])]
                    audits[audit_key] = ai.audit_short(candidate, cut_words, preview) if preview else {"keep": False}
                    write_json(audits_path, {**cache_key, "cuts_version": 4, "audit_version": 3, "answers": audits})
                if isinstance(audits[audit_key], dict) and audits[audit_key].get("keep") is True:
                    refined.append({**candidate, "output_number": output_number, "render_name": f"short-v7-{output_number:02d}"})
                continue
            region = (max(0, candidate["relative_start"] - 20), min(editorial_end - game["gameplay_start"], candidate["relative_end"] + 45))
            frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-cuts-{region[0]:.2f}-{region[1]:.2f}"
            sampled = sorted(frame_dir.glob("frame_*.jpg"))
            if not sampled:
                sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=game["gameplay_start"] + region[0], end=game["gameplay_start"] + region[1])]
            positions = sorted({round(step * (len(sampled) - 1) / 21) for step in range(22)}) if sampled else []
            frames = [{"timestamp": region[0] + position, "path": str(sampled[position])} for position in positions]
            relevant_words = [word for word in words if region[0] <= float(word.get("start", -1)) < region[1]]
            key = f"{candidate['source_start']:.2f}-{candidate['source_end']:.2f}"
            if key not in cuts:
                cuts[key] = ai.refine_short(candidate, relevant_words, frames)
                write_json(cuts_path, {**cache_key, "cuts_version": 4, "answers": cuts})
            cut = _retime_short(candidate, cuts[key], relevant_words, frames, game["gameplay_start"], region)
            if cut:
                audit_key = f"{cut['source_start']:.2f}-{cut['source_end']:.2f}"
                if audit_key not in audits:
                    within = [(region[0] + index, path) for index, path in enumerate(sampled)
                              if cut["relative_start"] <= region[0] + index < cut["relative_end"]]
                    positions = sorted({round(step * (len(within) - 1) / 11) for step in range(12)}) if within else []
                    preview = [{"timestamp": within[index][0], "path": str(within[index][1])} for index in positions]
                    cut_words = [word for word in relevant_words if cut["relative_start"] <= float(word["start"]) < cut["relative_end"]]
                    audits[audit_key] = ai.audit_short(cut, cut_words, preview) if preview else {"keep": False}
                    write_json(audits_path, {**cache_key, "cuts_version": 4, "audit_version": 3, "answers": audits})
                if isinstance(audits[audit_key], dict) and audits[audit_key].get("keep") is True:
                    refined.append({**cut, "output_number": output_number, "render_name": f"short-v7-{output_number:02d}"})
        selected = refined
    write_json(output, {**cache_key, "evidence_version": 2, "cuts_version": 4, "output_version": 9, "callback_version": 1, "candidates": selected})
    return selected


def _ass_time(seconds: float) -> str:
    centiseconds = round(max(0, seconds) * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def _caption_words(block: dict) -> list[dict]:
    if block.get("words"):
        return block["words"]
    tokens = str(block["text"]).split()
    weights = [max(1, len(token.strip(".,!?¡¿:;"))) for token in tokens]
    duration = float(block["end"]) - float(block["start"])
    cursor = float(block["start"])
    words = []
    for token, weight in zip(tokens, weights):
        end = cursor + duration * weight / sum(weights)
        words.append({"text": token, "start": cursor, "end": end})
        cursor = end
    return words


def _ass_text(words: list[dict], active: int, font: ImageFont.FreeTypeFont) -> list[str]:
    escaped = [
        str(word["text"]).upper().replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        for word in words
    ]
    split = None
    if len(" ".join(escaped)) > 22 and len(escaped) >= 4:
        split = min(
            range(1, len(escaped)),
            key=lambda index: abs(len(" ".join(escaped[:index])) - len(" ".join(escaped[index:]))),
        )
    lines = [escaped[:split], escaped[split:]] if split is not None else [escaped]
    rendered = []
    gap = font.getlength(" ") + 8
    caption_y = 652
    for line_index, line in enumerate(lines):
        widths = [
            font.getlength(word) * (116 / 96 if index == active else 1)
            for index, word in enumerate(line, start=split if line_index else 0)
        ]
        x = 540 - (sum(widths) + gap * (len(line) - 1)) / 2
        y = caption_y + (line_index - (len(lines) - 1) / 2) * 80
        for line_position, (word, width) in enumerate(zip(line, widths)):
            index = line_position + (split if line_index else 0)
            colour = "&H000000FF&" if index == active else "&H00FFFFFF&"
            size = 116 if index == active else 96
            center = x + width / 2
            bounce_x = 540 + (center - 540) * 1.01
            bounce_y = caption_y + (y - caption_y) * 1.01
            rendered.append(
                rf"{{\an5\move({bounce_x:.1f},{bounce_y:.1f},{center:.1f},{y:.1f},0,120)"
                rf"\1c{colour}\fs{size}\fscx101\fscy101\t(0,120,\fscx100\fscy100)}}{word}"
            )
            x += width + gap
    return rendered


def _validate_caption_blocks(blocks: list[dict], duration: float) -> None:
    previous_end = 0.0
    for block in blocks:
        start, end = float(block["start"]), float(block["end"])
        if not str(block.get("text", "")).strip() or start < previous_end or end <= start or end > duration + 0.01:
            raise ValueError(f"Invalid caption block: {block}")
        words = _caption_words(block)
        if not words or any(
            not str(word.get("text", "")).strip()
            or float(word["start"]) < start
            or float(word["end"]) <= float(word["start"])
            or float(word["end"]) > end + 0.01
            for word in words
        ) or any(float(right["start"]) < float(left["end"]) for left, right in zip(words, words[1:])):
            raise ValueError(f"Invalid caption words: {block}")
        previous_end = end


def _write_short_subtitles(vod_dir: Path, candidate: dict) -> Path:
    segments = candidate.get("segments") or [candidate]
    review = vod_dir / "short-captions.reviewed.json"
    reviewed = read_json(review) if review.exists() else {}
    if reviewed.get("segments") == segments and reviewed.get("segments") is not None:
        subtitle_blocks = reviewed.get("blocks", [])
    elif len(segments) == 1 and not candidate.get("segments") and reviewed.get("source_start") == candidate["source_start"] and reviewed.get("source_end") == candidate["source_end"]:
        subtitle_blocks = reviewed.get("blocks", [])
    else:
        subtitle_blocks = []
        offset = 0.0
        words = transcript_words(vod_dir)
        for segment in segments:
            selected = [word for word in words if float(word.get("start", 0)) < segment["relative_end"] and float(word.get("end", 0)) >= segment["relative_start"]]
            for block in _subtitle_blocks(selected, segment):
                block["start"] += offset
                block["end"] += offset
                for word in block["words"]:
                    word["start"] += offset
                    word["end"] += offset
                subtitle_blocks.append(block)
            offset += segment["relative_end"] - segment["relative_start"]
    duration = sum(segment["source_end"] - segment["source_start"] for segment in segments)
    _validate_caption_blocks(subtitle_blocks, duration)
    # Pillow's em is larger than libass's visible advances at \fs96.
    font = ImageFont.truetype(str(FILE), 60)
    events = []
    for block in subtitle_blocks:
        words = _caption_words(block)
        for index, word in enumerate(words):
            start = float(word["start"])
            end = float(words[index + 1]["start"]) if index + 1 < len(words) else float(block["end"])
            if end - start < 0.01:
                continue
            events.extend(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{text}"
                for text in _ass_text(words, index, font)
            )
    elapsed = 0.0
    for previous, following in zip(segments, segments[1:]):
        elapsed += previous["source_end"] - previous["source_start"]
        gap = following["source_start"] - previous["source_end"]
        if gap >= 60:
            label = f"{round(gap / 60)} min después"
            events.append(f"Dialogue: 1,{_ass_time(elapsed)},{_ass_time(elapsed + 1.6)},Editor,,0,0,0,,{{\\an5\\pos(540,960)}}{label}")
    output = vod_dir / "short.ass"
    output.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{ASS_NAME},96,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1\n"
        f"Style: Editor,{ASS_NAME},78,&H00FFA95B&,&H00FFA95B&,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,6,1,5,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events)
        + "\n",
        encoding="utf-8",
    )
    return output


def _subtitle_blocks(words: list[dict], candidate: dict) -> list[dict]:
    blocks: list[dict] = []
    group: list[dict] = []

    def flush() -> None:
        if not group:
            return
        start = max(0, float(group[0]["start"]) - candidate["relative_start"])
        end = min(
            candidate["relative_end"] - candidate["relative_start"],
            max(start + 0.1, float(group[-1]["end"]) - candidate["relative_start"]),
        )
        text = " ".join(str(word["word"]).strip() for word in group)
        step = min(0.01, (end - start) / (len(group) + 1))
        starts = []
        cursor = start
        for index, word in enumerate(group):
            latest = end - step * (len(group) - index)
            word_start = min(max(cursor, float(word["start"]) - candidate["relative_start"]), latest)
            starts.append(word_start)
            cursor = word_start + step
        timed_words = [
            {"text": str(word["word"]).strip(), "start": word_start, "end": starts[index + 1] if index + 1 < len(starts) else end}
            for index, (word, word_start) in enumerate(zip(group, starts))
        ]
        if blocks and blocks[-1]["end"] > start:
            blocks[-1]["end"] = start
            blocks[-1]["words"][-1]["end"] = start
        blocks.append({"start": start, "end": end, "text": text, "words": timed_words})
        group.clear()

    for word in words:
        text = str(word.get("word", "")).strip()
        if not text:
            continue
        proposed = " ".join([*(str(item["word"]).strip() for item in group), text])
        if group and (
            float(word["start"]) - float(group[-1]["end"]) > 0.65
            or len(group) >= 6
            or len(proposed) > 32
            or float(word["start"]) - float(group[0]["start"]) > 2.2
        ):
            flush()
        group.append(word)
    flush()
    return blocks


def render_short(vod_dir: Path, candidate_index: int = 0) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    candidates = short_candidates(vod_dir)
    if not candidates:
        raise SystemExit("No short candidates are available")
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise SystemExit(f"Short candidate index out of range: {candidate_index}")
    candidate = candidates[candidate_index]
    reviewed_layout = vod_dir / "layout.reviewed.json"
    layout = read_json(reviewed_layout if reviewed_layout.exists() else vod_dir / "layout.json")
    subtitles = _write_short_subtitles(vod_dir, candidate)
    render_name = candidate.get("render_name") or f"short-{candidate.get('output_number', candidate_index + 1):02d}"
    output = vod_dir / "renders" / f"{render_name}.mp4"
    render_vertical(Path(manifest["source"]), candidate.get("segments") or [candidate],
                    layout["facecam"], layout.get("gameplay", [0.255, 0, 0.49, 1]), subtitles, output,
                    layout.get("hud"))
    return output
