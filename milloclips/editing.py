from __future__ import annotations

import json
import hashlib
import math
import re
import struct
import tempfile
import unicodedata
import wave
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
from yt_dlp import YoutubeDL

from .ai import AI, SHORT_MAX_SECONDS, MAX_REQUEST_IMAGES
from openai import OpenAIError
from .fonts import ASS_NAME, FILE
from .io import read_json, write_json
from .media import TITLE_DISSOLVE, TITLE_DURATION, TITLE_MUSIC_LEAD, TITLE_MUSIC_START, duration, extract_audio, ffmpeg, intro_duration, media_info, render_long_video, render_segments, render_vertical, run_ffmpeg, sample_boundary_frames, sample_frames, title_start


ACCENT = (234, 255, 32)


def _bounded_frames(frames: list[dict], anchors: list[float] = ()) -> list[dict]:
    """Keep event proof plus an even overview, never an unbounded image request."""
    if len(frames) <= MAX_REQUEST_IMAGES:
        return frames
    chosen = {min(range(len(frames)), key=lambda i: abs(frames[i]["timestamp"] - time)) for time in anchors}
    if len(chosen) > MAX_REQUEST_IMAGES:
        raise ValueError("Too many distinct visual anchors for one bounded request")
    remaining = [i for i in range(len(frames)) if i not in chosen]
    slots = MAX_REQUEST_IMAGES - len(chosen)
    chosen.update(remaining[round(i * (len(remaining) - 1) / max(1, slots - 1))] for i in range(slots))
    return [frames[i] for i in sorted(chosen)]


def _cache_matches(data: dict, **expected: object) -> bool:
    return all(data.get(key) == value for key, value in expected.items())


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _file_fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else None


def _media_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _transcript_edits(vod_dir: Path) -> dict:
    return {
        name: _file_fingerprint(vod_dir / name)
        for name in ("transcript.reviewed.json", "short-transcript.json", "short-transcript.reviewed.json")
    }


def _source_identity(source: Path) -> dict:
    stat = source.stat() if source.exists() else None
    return {"path": str(source.resolve()), "size": stat.st_size if stat else None, "mtime_ns": stat.st_mtime_ns if stat else None}


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


def _validate_short_candidates(candidates: object, game_start: float, game_end: float, media_duration: float) -> list[dict]:
    if not isinstance(candidates, list) or len(candidates) > 2:
        raise ValueError("Short output must contain zero, one, or two candidates")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Invalid short candidate")
        segments = candidate.get("segments") or [candidate]
        if not isinstance(segments, list) or not 1 <= len(segments) <= 4:
            raise ValueError("Short candidates require one to four segments")
        previous_end = -1.0
        previous_relative_end = -1.0
        total = 0.0
        offsets = []
        for segment in segments:
            if not isinstance(segment, dict) or any(type(segment.get(key)) not in (int, float) for key in ("source_start", "source_end", "relative_start", "relative_end")):
                raise ValueError("Invalid short segment")
            source_start, source_end = float(segment["source_start"]), float(segment["source_end"])
            relative_start, relative_end = float(segment["relative_start"]), float(segment["relative_end"])
            if not all(math.isfinite(value) for value in (source_start, source_end, relative_start, relative_end)) \
                    or source_start < previous_end or relative_start < previous_relative_end \
                    or not game_start <= source_start < source_end <= min(game_end, media_duration) \
                    or not 0 <= relative_start < relative_end <= game_end - game_start \
                    or abs((source_end - source_start) - (relative_end - relative_start)) > 0.05:
                raise ValueError("Short segments must be finite, positive, chronological, and non-overlapping")
            previous_end = source_end
            previous_relative_end = relative_end
            total += source_end - source_start
            offsets.append(source_start - relative_start)
        if not 8 - 1e-6 <= total <= SHORT_MAX_SECONDS + 1e-6:
            raise ValueError(f"Short duration must be between 8 and {SHORT_MAX_SECONDS} seconds (got {total:.6f})")
        if "payoff_end" in candidate:
            payoff = candidate.get("payoff_evidence", {})
            start, end = payoff.get("time"), candidate["payoff_end"]
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end) or end < start
                    or not any(part["relative_start"] <= start < part["relative_end"]
                               and end <= part["relative_end"] + 0.01 for part in segments)):
                raise ValueError("Short must retain the complete payoff, not just its starting anchor")
        first, last = segments[0], segments[-1]
        if any(abs(offset - game_start) > 0.05 for offset in offsets) or any(
            type(candidate.get(key)) not in (int, float) for key in ("source_start", "source_end", "relative_start", "relative_end")
        ) or abs(float(candidate["source_start"]) - float(first["source_start"])) > 0.05 \
                or abs(float(candidate["source_end"]) - float(last["source_end"])) > 0.05 \
                or abs(float(candidate["relative_start"]) - float(first["relative_start"])) > 0.05 \
                or abs(float(candidate["relative_end"]) - float(last["relative_end"])) > 0.05 \
                or abs(float(candidate.get("duration", total)) - total) > 0.05:
            raise ValueError("Short candidate timestamps do not match its segments or selected game")
    return candidates


def short_candidates(vod_dir: Path) -> list[dict]:
    reviewed = vod_dir / "short-candidates.reviewed.json"
    raw = read_json(vod_dir / "short-candidates.json")
    if raw.get("scope") == "vod":
        workspace = Path(raw["workspace"])
        if raw.get("signature") != _file_fingerprint(workspace / "short-candidates.json"):
            raise SystemExit("Whole-VOD shorts changed; run find-shorts again")
        if _source_identity(Path(read_json(workspace / "manifest.json")["source"])) != _source_identity(Path(read_json(vod_dir / "manifest.json")["source"])):
            raise ValueError("Whole-VOD shorts belong to a different source")
        return short_candidates(workspace)
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    exclusions_path = vod_dir / "short-exclusions.reviewed.json"
    exclusions_review = read_json(exclusions_path) if exclusions_path.exists() else {}
    exclusions = exclusions_review.get("ranges", []) if exclusions_review.get("game_id", game["id"]) == game["id"] else None
    manifest = read_json(vod_dir / "manifest.json")
    source_identity = _source_identity(Path(manifest["source"]))
    if not _cache_matches(raw, version=8, output_version=21, evidence_version=7, cuts_version=10, callback_version=2,
                          game_id=game["id"], gameplay_start=game["gameplay_start"], editorial_end=editorial_end,
                          exclusions=exclusions, transcript_edits=_transcript_edits(vod_dir),
                          beats=_file_fingerprint(vod_dir / "beats.json"),
                          source_identity=source_identity):
        raise SystemExit("short-candidates.json is stale; run find-shorts again")
    edit_end = raw.get("edit_end", editorial_end)
    maximum_end = min(manifest["duration"], editorial_end if "result_end" in game else
                      min(game.get("postgame_end", editorial_end + 15), editorial_end + 15))
    if type(edit_end) not in (int, float) or not math.isfinite(edit_end) or not editorial_end <= edit_end <= maximum_end:
        raise ValueError("Short ending exceeds the bounded selected-game result context")
    if reviewed.exists():
        correction = read_json(reviewed)
        if correction.get("version") != raw.get("version") or correction.get("game_id") != raw.get("game_id") \
                or correction.get("raw_signature") != _fingerprint(raw.get("candidates", [])):
            raise SystemExit("short-candidates.reviewed.json is stale or malformed; update its version, game_id, and raw_signature or remove it")
        return _validate_short_candidates(correction.get("candidates"), game["gameplay_start"], edit_end, manifest["duration"])
    return _validate_short_candidates(raw.get("candidates"), game["gameplay_start"], edit_end, manifest["duration"])


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


def analyze(vod_dir: Path, ai: AI, force: bool = False, beat_seconds: float = 15,
            transcript_only: bool = False) -> list[dict]:
    output = vod_dir / "beats.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    cache_key = {
        "game_id": game["id"],
        "gameplay_start": game["gameplay_start"],
        "editorial_end": editorial_end,
        "transcript_edits": _transcript_edits(vod_dir),
        "source_identity": _source_identity(source),
    }
    cached = read_json(output) if output.exists() else {}
    if output.exists() and not force:
        if _cache_matches(cached, **cache_key, target_beat_seconds=beat_seconds):
            return cached["beats"]
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
    if transcript_only:
        # Between-game scouting needs timed speech, not another visual classifier.
        classified = [{**beat, "type": "REACTION" if beat["text"] else "TRAVEL",
                       "importance": 2 if beat["text"] else 1, "keep": bool(beat["text"]),
                       "reason": "Unranked speech evidence for whole-stream discovery"} for beat in beats]
        write_json(output, {**cache_key, "target_beat_seconds": beat_seconds, "beats": classified})
        return classified
    batch_path = vod_dir / "beat-classifications.json"
    batch_cache = read_json(batch_path) if batch_path.exists() else {}
    analysis_model = getattr(ai, "model", "gpt-5-nano")
    answers = batch_cache.get("answers", {}) if _cache_matches(batch_cache, **cache_key, model=analysis_model) and not force else {}
    batches = []
    for offset in range(0, len(beats), 16):
        batch = beats[offset : offset + 16]
        start = batch[0]["start"]
        end = batch[-1]["end"]
        batch_frames = [Path(min(frames, key=lambda frame: abs(
            frame["timestamp"] - game["gameplay_start"] - (beat["start"] + beat["end"]) / 2))["path"])
            for beat in batch] if frames else []
        key = _fingerprint({"beats": batch, "frames": [str(path) for path in batch_frames]})
        batches.append((key, batch, batch_frames))
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(ai.classify_beats, batch, images): key for key, batch, images in batches if key not in answers}
        for future in as_completed(pending):
            answers[pending[future]] = future.result()
            write_json(batch_path, {**cache_key, "model": analysis_model, "answers": answers})
    for key, _, _ in batches:
        classified.extend(answers[key])

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


def _long_scene_ranges(beats: list[dict], model_plan: dict) -> list[dict]:
    by_id = {beat["id"]: index for index, beat in enumerate(beats)}
    scenes = []
    proposals = sorted(
        (proposal for proposal in model_plan.get("scenes", []) if isinstance(proposal, dict)
         and type(proposal.get("start_beat_id")) is int and proposal["start_beat_id"] in by_id),
        key=lambda proposal: by_id[proposal["start_beat_id"]],
    )
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        start_id, end_id = proposal.get("start_beat_id"), proposal.get("end_beat_id")
        if type(start_id) is not int or type(end_id) is not int or start_id not in by_id or end_id not in by_id:
            continue
        first, last = by_id[start_id], by_id[end_id]
        if first > last:
            continue
        scenes.append({
            "start_beat_id": start_id,
            "end_beat_id": end_id,
            "first_index": first,
            "last_index": last,
            "start": float(beats[first]["start"]),
            "end": float(beats[last]["end"]),
            "purpose": str(proposal.get("purpose", "selected story moment"))[:240],
            "required": bool(proposal.get("required", False)),
        })
    if not scenes:
        selected = set(model_plan.get("selected_ids", []))
        for index, beat in enumerate(beats):
            if beat["id"] not in selected:
                continue
            if scenes and index == scenes[-1]["last_index"] + 1:
                scenes[-1].update(end_beat_id=beat["id"], last_index=index, end=float(beat["end"]))
            else:
                scenes.append({"start_beat_id": beat["id"], "end_beat_id": beat["id"], "first_index": index,
                               "last_index": index, "start": float(beat["start"]), "end": float(beat["end"]),
                               "purpose": "selected story moment", "required": False})

    if scenes:
        last_gameplay = max((index for index, beat in enumerate(beats) if beat.get("phase") == "IN_GAME"), default=None)
        final = scenes[-1]
        if last_gameplay is not None and final["last_index"] < last_gameplay and not final["required"]:
            first = last_gameplay
            while first > final["last_index"] + 1 and beats[last_gameplay]["end"] - beats[first - 1]["start"] <= 90:
                first -= 1
            scenes.append({"start_beat_id": beats[first]["id"], "end_beat_id": beats[last_gameplay]["id"],
                           "first_index": first, "last_index": last_gameplay, "start": float(beats[first]["start"]),
                           "end": float(beats[last_gameplay]["end"]), "purpose": "actual final fight, result, and reaction",
                           "required": True})
        scenes[0]["required"] = True
        scenes[-1]["required"] = True
        scenes[-1]["final_scene"] = True
    if not scenes or any(right["start"] < left["end"] for left, right in zip(scenes, scenes[1:])):
        raise ValueError("Long scene selection needs review: empty or overlapping story units")
    return scenes


def _avoid_split_word(start: float, end: float, words: list[dict], region: tuple[float, float]) -> tuple[float, float]:
    if any(float(word.get("start", -1)) < start < float(word.get("end", -1)) for word in words):
        pauses = [
            float(right["start"])
            for left, right in zip(words, words[1:])
            if region[0] <= float(right.get("start", -1)) <= start
            and float(right.get("start", 0)) - float(left.get("end", 0)) >= 0.45
        ]
        spanning = next(word for word in words if float(word.get("start", -1)) < start < float(word.get("end", -1)))
        start = max(region[0], (pauses[-1] if pauses else float(spanning["start"])) - 0.1)
    while spanning := [word for word in words if float(word.get("start", -1)) < start < float(word.get("end", -1))]:
        earlier = max(region[0], min(float(word["start"]) for word in spanning))
        if earlier == start:
            break
        start = earlier
    split_exit = any(float(word.get("start", -1)) < end < float(word.get("end", -1)) for word in words)
    while spanning := [word for word in words if float(word.get("start", -1)) < end < float(word.get("end", -1))]:
        later = min(region[1], max(float(word["end"]) for word in spanning))
        if later == end:
            break
        end = later
    if split_exit and end + 0.1 <= region[1] and not any(
        float(word.get("start", -1)) < end + 0.1 < float(word.get("end", -1)) for word in words
    ):
        end += 0.1
    if any(float(word.get("start", -1)) < boundary < float(word.get("end", -1))
           for boundary in (start, end) for word in words):
        raise ValueError("Inspected context ends inside a word; more context is needed")
    return start, end


def _complete_speech_exit(end: float, words: list[dict], region: tuple[float, float]) -> float:
    """Finish a continuing exchange within inspected context, for either format."""
    left = [word for word in words if float(word["end"]) <= end]
    right = [word for word in words if float(word["start"]) >= end]
    if left and right and float(right[0]["start"]) - float(left[-1]["end"]) < 0.45:
        pauses = [float(a["end"]) + 0.1 for a, b in zip(words, words[1:])
                  if end <= float(a["end"]) <= region[1] - 0.1
                  and float(b["start"]) - float(a["end"]) >= 0.45]
        if not pauses:
            raise ValueError("Exit cuts a continuing sentence/exchange; more context is needed")
        return pauses[0]
    return end


def _long_boundary_segments(answer: object, region: tuple[float, float], words: list[dict], frames: list[dict],
                            *, windows: list[dict] | None = None, mock: bool = False, final_scene: bool = False,
                            complete_combat: bool = False, constructed: bool = False) -> list[dict]:
    """Validate event anchors and revalidate ranges after word-boundary protection."""
    if not isinstance(answer, dict) or any(answer.get(key) is not False for key in ("needs_earlier_context", "needs_later_context")):
        raise ValueError("Event setup or outcome is unresolved")
    parts = answer.get("segments")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 6 or (windows is not None and len(parts) != len(windows)):
        raise ValueError("Expected one to six retained event ranges")
    if complete_combat and not any(isinstance(part, dict) and part.get("event_kind") == "combat" for part in parts):
        raise ValueError("The selected combat scene must retain its complete encounter, not become a speech-only edit")
    result = []
    previous_end = region[0]
    for index, part in enumerate(parts):
        numeric = ("start", "end", "action_start", "outcome_time", "reaction_end")
        if not isinstance(part, dict) or any(type(part.get(key)) not in (int, float) or not math.isfinite(part[key]) for key in numeric):
            raise ValueError("Missing finite event boundaries or anchors")
        start, end = part["start"], part["end"]
        action, outcome, reaction = (part[key] for key in numeric[2:])
        if (not region[0] <= start <= action < outcome <= end <= region[1] or not action <= reaction <= end
                or start < previous_end):
            raise ValueError("Event ranges/anchors must be complete, chronological and non-overlapping")
        if part.get("event_kind") not in {"combat", "speech"} or (part["event_kind"] == "combat" and start >= action):
            raise ValueError("Combat must have a pre-action entry; speech requires a premise")
        if any(not isinstance(part.get(key), str) or not part[key].strip() for key in ("start_reason", "end_reason")):
            raise ValueError("Entry and exit need editorial reasons")
        downtime = part.get("downtime_start")
        if part.get("downtime_kind") not in {"recall", "shopping", "travel", "waiting", "none"}:
            raise ValueError("Unknown downtime kind")
        if downtime is not None and (type(downtime) not in (int, float) or not math.isfinite(downtime)
                                     or not outcome <= downtime <= region[1]):
            raise ValueError("Invalid downtime anchor")
        if (downtime is None) != (part["downtime_kind"] == "none"):
            raise ValueError("Downtime kind and anchor disagree")
        phrase_end = reaction
        if not mock:
            evidence = part.get("evidence", {})
            for key in ("action_start", "outcome_time", "reaction_end", *(["downtime_start"] if downtime is not None else [])):
                item = evidence.get(key) if isinstance(evidence, dict) else None
                if not isinstance(item, dict) or type(item.get("time")) not in (int, float) or abs(item["time"] - part[key]) > 0.01:
                    raise ValueError(f"Missing evidence for {key}")
                if complete_combat and part["event_kind"] == "combat" and key in {"action_start", "outcome_time"} and item.get("kind") != "visual":
                    raise ValueError(f"Combat {key} must cite a visible engagement/resolved encounter, not a spoken hook or punchline")
                if item.get("kind") == "speech":
                    span = _spoken_span(str(item.get("quote") or item.get("detail", "")), words, max(region[0], item["time"] - 0.5), region[1], minimum_tokens=1)
                    if span is None or abs(span[0] - item["time"]) > 0.5:
                        raise ValueError(f"Unmatched speech anchor: {key}")
                    if key == "reaction_end":
                        phrase_end = max(phrase_end, span[1])
                elif item.get("kind") != "visual" or len(str(item.get("detail", "")).strip()) < 12 or not any(
                    abs(frame["timestamp"] - item["time"]) <= 0.01 for frame in frames
                ):
                    raise ValueError(f"Visual anchor is not an inspected frame: {key}")
        # A reaction can precede the result banner, and coarse cuts may end inside its quote.
        if max(phrase_end, outcome) >= end:
            end = min(region[1], max(phrase_end, outcome) + 0.1)
        start, end = _avoid_split_word(start, end, words, region)
        # Word-safe gaps are not necessarily sentence-safe: finish a continuing exchange.
        end = _complete_speech_exit(end, words, region)
        final_exit = final_scene and index == len(parts) - 1 and not mock
        if final_exit:
            # Leave a real speech-free beat on the result, never mute unfinished dialogue.
            end = max(end, outcome + 1, phrase_end + 1)
            for word in sorted(words, key=lambda item: float(item["start"])):
                if start <= float(word["start"]) < end:
                    end = max(end, float(word["end"]) + 1)
            if end > region[1]:
                raise ValueError("Final result needs more context for a one-second speech-free ending")
        if windows is not None and not (windows[index]["start"][0] <= start <= windows[index]["start"][1]
                                       and windows[index]["end"][0] <= end <= windows[index]["end"][1]):
            raise ValueError(f"Cut {start:.3f}-{end:.3f} lies outside its inspected dense boundary window {windows[index]}")
        if windows is not None and not mock:
            inspected_times = [frame["timestamp"] for frame in frames]
            speech_times = [float(word[key]) for word in words for key in ("start", "end")]
            if constructed:
                # Native frame/speech-safe construction uses known, bounded padding.
                inspected_times += [frame["timestamp"] + 0.35 for frame in frames]
                speech_times += [time for word in words for time in (float(word["start"]) - 0.1,
                                 float(word["start"]) - 1 / 30, float(word["end"]) + 0.1)]
            for boundary in (part["start"], part["end"]):
                if not any(abs(boundary - time) <= 1 / 30 for time in [*inspected_times, *speech_times, *region]):
                    raise ValueError("Precise cut must cite an inspected frame or word boundary")
        if (not previous_end <= start <= action < outcome < end <= region[1]
                or not action <= reaction < end or phrase_end > end):
            raise ValueError("Word adjustment invalidated event boundaries")
        # A useful spoken reaction may extend past recall onset; routine dead tails may not.
        if not final_exit and downtime is not None and end > max(downtime, phrase_end + 0.5) + 0.25:
            raise ValueError("Cut retains downtime after its completed reaction")
        result.append({**part, "start": start, "end": end})
        previous_end = end
    return result


def _deduplicate_long_events(cuts: list[dict]) -> list[dict]:
    """Keep covered footage once; shared lead-ins may end at a prior approved exit."""
    unique = []
    for cut in sorted(cuts, key=lambda part: part["source_start"]):
        if unique and cut["source_start"] < unique[-1]["source_end"]:
            previous = unique[-1]
            if previous.get("boundary_note") or cut.get("boundary_note"):
                end = max(previous["source_end"], cut["source_end"])
                offset = previous["source_start"] - previous["start"]
                unique[-1] = {**previous, "source_end": end, "end": end - offset, "relative_end": end - offset,
                              "required": previous.get("required", False) or cut.get("required", False),
                              "boundary_note": previous.get("boundary_note") or cut["boundary_note"],
                              "scene_ids": list(dict.fromkeys([*previous.get("scene_ids", [previous["scene_id"]]),
                                                               *cut.get("scene_ids", [cut["scene_id"]])]))}
                continue
            contained = (previous["source_start"] <= cut["source_start"] and cut["source_end"] <= previous["source_end"]) or (
                cut["source_start"] <= previous["source_start"] and previous["source_end"] <= cut["source_end"])
            offset = cut["source_start"] - cut["start"]
            previous_offset = previous["source_start"] - previous["start"]
            same_event = (previous["event_kind"] == cut["event_kind"]
                          and abs(previous_offset + previous["action_start"] - offset - cut["action_start"]) <= 2
                          and abs(previous_offset + previous["outcome_time"] - offset - cut["outcome_time"]) <= 2)
            if not contained and same_event:
                # Independent 2s scouts may identify the same event on neighboring decoded frames.
                end = max(previous["source_end"], cut["source_end"])
                cut = {**previous, "source_end": end, "end": end - previous_offset, "relative_end": end - previous_offset,
                       "required": previous.get("required", False) or cut.get("required", False),
                       "scene_ids": list(dict.fromkeys([*previous.get("scene_ids", [previous["scene_id"]]),
                                                        *cut.get("scene_ids", [cut["scene_id"]])]))}
                unique[-1] = cut
                continue
            if not contained and previous["source_end"] < offset + cut["action_start"]:
                # The overlapping setup already plays in the outgoing event; retain pre-action entry.
                start = previous["source_end"] - offset
                unique.append({**cut, "source_start": previous["source_end"], "start": start, "relative_start": start,
                               "start_reason": "Continue after the prior approved exit; shared setup is shown once. " + cut["start_reason"]})
                continue
            if not contained:
                raise ValueError(f"Refined events overlap: {previous['scene_id']} and {cut['scene_id']}")
            kept = max((previous, cut), key=lambda part: part["source_end"] - part["source_start"])
            unique[-1] = {**kept, "required": previous.get("required", False) or cut.get("required", False),
                          "scene_ids": list(dict.fromkeys([*previous.get("scene_ids", [previous["scene_id"]]),
                                                           *cut.get("scene_ids", [cut["scene_id"]])]))}
        else:
            unique.append(cut)
    return unique


def _long_cold_open(answer: dict, parts: list[dict], words: list[dict], region: tuple[float, float], windows: dict) -> tuple[float, float]:
    start, end = answer.get("cold_open_start"), answer.get("cold_open_end")
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or not 5 <= end - start <= 20
            or not any(part["start"] <= start < end <= part["end"] for part in parts)):
        raise ValueError("Missing valid complete cold open inside a retained event")
    start, end = _avoid_split_word(start, end, words, region)
    if (not 5 <= end - start <= 20 or not any(part["start"] <= start < end <= part["end"] for part in parts)
            or not windows["start"][0] <= start <= windows["start"][1]
            or not windows["end"][0] <= end <= windows["end"][1]):
        raise ValueError("Cold-open word adjustment left its retained event or exceeded 20s; select a complete phrase boundary")
    return start, end


def _short_frame_decision(answer: object, frames: list[dict], words: list[dict] | None = None) -> object:
    """Convert observed frame indices to source times; keep cached model answers raw."""
    if not isinstance(answer, dict) or not isinstance(answer.get("segments"), list):
        return answer
    parts = []
    for part in answer["segments"]:
        if not isinstance(part, dict) or "start_frame" not in part:
            parts.append({**part, "evidence": dict(part.get("evidence") or {})} if isinstance(part, dict) else part)
            continue
        resolved = {**{key: value for key, value in part.items() if not key.endswith("_frame")},
                    "evidence": {}, "downtime_start": None, "downtime_kind": "none"}
        for key, index_key in (("start", "start_frame"), ("end", "end_frame"), ("action_start", "action_frame"),
                               ("outcome_time", "outcome_frame"), ("reaction_end", "reaction_frame")):
            index = part.get(index_key)
            if type(index) is not int or not 0 <= index < len(frames):
                raise ValueError(f"{index_key} must identify an actual supplied image")
            resolved[key] = frames[index]["timestamp"]
            if key not in {"start", "end"}:
                detail_key = {"action_start": "action_detail", "outcome_time": "outcome_detail", "reaction_end": "reaction_detail"}[key]
                resolved["evidence"][key] = {"kind": "visual", "time": resolved[key], "detail": part.get(detail_key, "")}
        parts.append(resolved)
    retained = []
    for part in parts:
        if retained and isinstance(part, dict) and isinstance(retained[-1], dict) and type(part.get("start")) in (int, float) and type(retained[-1].get("end")) in (int, float) \
                and part["start"] <= retained[-1]["end"] + 0.01:
            previous = retained[-1]
            previous["end"] = max(previous["end"], part["end"])
            previous["outcome_time"] = part["outcome_time"]
            previous["reaction_end"] = part["reaction_end"]
            previous["end_reason"] = part.get("end_reason", "")
            previous["evidence"].update({key: part["evidence"][key] for key in ("outcome_time", "reaction_end")})
            if part.get("event_kind") == "combat" and previous.get("event_kind") != "combat":
                previous["event_kind"] = "combat"
                previous["action_start"] = part["action_start"]
                previous["evidence"]["action_start"] = part["evidence"]["action_start"]
        else:
            retained.append(part)
    for part in retained:
        if not isinstance(part, dict) or part.get("event_kind") != "combat" or "action_start" not in part:
            continue
        if part["start"] >= part["action_start"]:
            approach = [frame["timestamp"] for frame in frames if frame["timestamp"] < part["action_start"]]
            if not approach:
                return {**answer, "segments": retained, "needs_earlier_context": True}
            part["start"] = approach[-1]
        if not words or "outcome_time" not in part.get("evidence", {}):
            # Synthetic canonical decisions have no observed outcome evidence.
            continue
        # Finish only speech already underway at the resolved outcome. Do not
        # stretch a Short through renewed result complaints or next-match talk.
        outcome = part["outcome_time"]
        before = [word for word in words if float(word["start"]) <= outcome]
        after = [word for word in words if float(word["start"]) > outcome]
        last = before[-1] if before else None
        speaking = last is not None and (float(last["end"]) >= outcome or
                   bool(after) and float(after[0]["start"]) - float(last["end"]) < 0.45)
        if speaking:
            end = max(outcome, float(last["end"]))
            for word in after:
                if float(word["start"]) - end >= 0.45:
                    break
                end = max(end, float(word["end"]))
                last = word
            part["end"] = end + 0.1
            part["reaction_end"] = float(last["start"])
            part["evidence"]["reaction_end"] = {"kind": "speech", "time": part["reaction_end"], "quote": last["word"]}
        else:
            part["end"] = outcome + 0.35
            if after:
                part["end"] = min(part["end"], float(after[0]["start"]) - 1 / 30)
            part["reaction_end"] = outcome
            part["evidence"]["reaction_end"] = dict(part["evidence"]["outcome_time"])
        part["end_reason"] = "Resolved encounter and ongoing dialogue complete; omit renewed post-game talk"
    return {**answer, "segments": retained}


def _fit_short_event_budget(parts: list[dict], seconds: float, words: list[dict], frames: list[dict], selected_time: float,
                            windows: list[dict] | None = None) -> list[dict]:
    """Remove only surplus pre-action lead at an inspected frame or speech pause."""
    result = [{**part} for part in parts]
    excess = sum(part["end"] - part["start"] for part in result) - seconds
    pauses = [float(right["start"]) for left, right in zip(words, words[1:])
              if float(right["start"]) - float(left["end"]) >= 0.45]
    for index, part in enumerate(result):
        if excess <= 0:
            break
        if part.get("event_kind") != "combat":
            continue
        goal = part["start"] + excess
        limit = min(part["action_start"] - 1 / 30, selected_time - 0.25)
        candidates = set(pauses)
        for frame in frames:
            time = frame["timestamp"]
            if any(float(word["start"]) <= time < float(word["end"]) for word in words):
                continue
            left = [word for word in words if float(word["end"]) <= time]
            right = [word for word in words if float(word["start"]) >= time]
            if not left or not right or float(right[0]["start"]) - float(left[-1]["end"]) >= 0.45:
                candidates.add(time)
        choices = [time for time in candidates if goal <= time <= limit
                   and not any(float(word["start"]) < time < float(word["end"]) for word in words)
                   and (windows is None or windows[index]["start"][0] <= time <= windows[index]["start"][1])]
        if choices:
            start = min(choices)
            excess -= start - part["start"]
            part["start"] = start
            part["start_reason"] += "; removed surplus pre-action lead at a speech-safe inspected boundary"
    if excess > 0.01:
        raise ValueError(f"This scene has only {seconds:.2f} seconds available. Retain its selected premise, complete "
                         "engagement and resolved outcome; remove only nonessential lead or actual idle time.")
    return result


def _check_short_moments(parts: list[dict], moments: list[float]) -> None:
    if any(not any(part["start"] <= moment < part["end"] for part in parts) for moment in moments):
        raise ValueError("Keep the selected hook AND its evidenced payoff; the constructed event dropped a required moment")


def _refine_long_scenes(vod_dir: Path, ai: AI, game: dict, beats: list[dict], scenes: list[dict], cache_key: dict,
                        cold_open_beat_id: int | None, force: bool, *, require_intro_hint: bool = False,
                        short: bool = False) -> tuple[list[dict], dict | None]:
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    origin = game["gameplay_start"]
    words = transcript_words(vod_dir)
    pregame_path = vod_dir / "pregame-transcript.json"
    if pregame_path.exists():
        pregame = read_json(pregame_path)
        offset = pregame.get("audio_start", origin) - origin
        words = sorted([*words, *[{**word, "start": word["start"] + offset, "end": word["end"] + offset}
                                 for word in pregame.get("words", [])]], key=lambda word: word["start"])
    bounds = (max(-origin, min(beat["start"] for beat in beats)), min(manifest["duration"], cache_key["editorial_end"]) - origin)
    prefix = "short-event" if short else "long"
    path = vod_dir / f"{prefix}-cuts.json"
    cached = read_json(path) if path.exists() else {}
    answers = cached.get("answers", {}) if not force and _cache_matches(cached, **cache_key, cuts_version=2) else {}
    reviewed_path = vod_dir / f"{prefix}-cuts.reviewed.json"
    reviewed = read_json(reviewed_path) if reviewed_path.exists() else {}
    if reviewed and not _cache_matches(reviewed, **cache_key, cuts_version=2):
        raise ValueError(f"{prefix}-cuts.reviewed.json is stale; rebind corrections to the new boundary inputs")
    cuts = []
    cold_open = None

    def sampled(region: tuple[float, float], interval: float) -> list[dict]:
        tag = _fingerprint({"source": cache_key.get("source_identity"), "region": region, "interval": interval})
        frames = sample_boundary_frames(source, vod_dir / "frames" / "long-boundaries" / tag, interval,
                                        start=origin + region[0], end=origin + region[1])
        return [{**frame, "timestamp": frame["timestamp"] - origin} for frame in frames]

    for scene in scenes:
        if short:
            scene = {**scene, "format": "short", "remaining_seconds": scene.get("short_budget", SHORT_MAX_SECONDS) - sum(part["relative_end"] - part["relative_start"] for part in cuts)}
        scene_bounds = bounds
        scene_words = words
        if (scene.get("final_scene") or scene.get("ending_scene")) and "result_end" not in game:
            # Scanner gameplay end may precede the result animation; inspect only a bounded tail.
            context_end = min(manifest["duration"], game.get("postgame_end", cache_key["editorial_end"] + 15),
                              cache_key["editorial_end"] + 15)
            if context_end > cache_key["editorial_end"]:
                tail_path = vod_dir / "long-ending-transcript.json"
                tail_key = {"audio_start": cache_key["editorial_end"], "audio_end": context_end,
                            "source_identity": _source_identity(source), "mock": ai.mock}
                tail = read_json(tail_path) if tail_path.exists() else {}
                if not _cache_matches(tail, **tail_key):
                    audio = vod_dir / "long-ending.mp3"
                    extract_audio(source, audio, tail_key["audio_start"], context_end)
                    tail = {**ai.transcribe(audio, context_end - tail_key["audio_start"]), **tail_key}
                    write_json(tail_path, tail)
                offset = tail_key["audio_start"] - origin
                scene_words = sorted([*words, *[{**word, "start": word["start"] + offset, "end": word["end"] + offset}
                                               for word in tail.get("words", [])]], key=lambda word: word["start"])
                scene_bounds = (bounds[0], context_end - origin)
        key = scene.get("cache_id", f"{scene['start_beat_id']}:{scene['end_beat_id']}")
        signature_inputs = {"scene": scene, "cold_open": cold_open_beat_id}
        if scene.get("final_scene") or scene.get("ending_scene"):
            signature_inputs["ending_context_end"] = scene_bounds[1]
            signature_inputs["ending_policy"] = 2
        signature = _fingerprint(signature_inputs)
        entry = answers.get(key, {})
        if entry.get("scene_signature") != signature:
            entry = {"scene_signature": signature, "locate": []}
            answers[key] = entry
        is_cold = cold_open_beat_id is not None and scene["start_beat_id"] <= cold_open_beat_id <= scene["end_beat_id"]
        region = (max(scene_bounds[0], scene["start"] - 20), min(scene_bounds[1], scene["end"] + 20))
        if short and scene.get("known_events"):
            region = (max(scene_bounds[0], min(region[0], min(part["action_start"] for part in scene["known_events"]) - 2)),
                      min(scene_bounds[1], max(region[1], max(part["outcome_time"] for part in scene["known_events"]) + 2)))
        complete_combat = short and scene.get("expected_event_kind") == "combat"
        try:
            locate_feedback = None
            locate_attempts = 4 if short else 2
            for attempt in range(locate_attempts):
                frames = _bounded_frames(sampled(region, 2), scene.get("required_moment_times", []))
                if short:
                    # Preserve transient verified banners between coarse sample times.
                    frames = sorted({frame["timestamp"]: frame for frame in
                                     [*frames, *[frame for frame in scene.get("verified_frames", [])
                                                 if region[0] <= frame["timestamp"] <= region[1]]]}.values(),
                                    key=lambda frame: frame["timestamp"])
                nearby_words = [word for word in scene_words if region[0] <= word["start"] < region[1]]
                nearby_beats = [beat for beat in beats if beat["start"] < region[1] and beat["end"] > region[0]]
                locate_input = _fingerprint({"frames": frames, "words": nearby_words, "region": region, "feedback": locate_feedback})
                locate_inputs = entry.setdefault("locate_inputs", {}) if short else {}
                if len(entry["locate"]) <= attempt or (short and locate_inputs.get(str(attempt)) != locate_input):
                    decision = ai.refine_long_scene(
                        {**scene, "allowed_region": list(region), "boundary_pass": "locate", "validation_feedback": locate_feedback}, nearby_beats, nearby_words, frames, cold_open=is_cold)
                    if len(entry["locate"]) <= attempt:
                        entry["locate"].append(decision)
                    else:
                        entry["locate"][attempt] = decision
                    locate_inputs[str(attempt)] = locate_input
                    write_json(path, {**cache_key, "cuts_version": 2, "answers": answers})
                located = entry["locate"][attempt]
                earlier = isinstance(located, dict) and located.get("needs_earlier_context") is True
                later = isinstance(located, dict) and located.get("needs_later_context") is True
                if not earlier and not later:
                    try:
                        if short:
                            located = _short_frame_decision(located, frames, nearby_words)
                        checked_locate = _long_boundary_segments(located, region, nearby_words, frames, mock=ai.mock,
                                                                final_scene=scene.get("final_scene", False), complete_combat=complete_combat)
                        if short:
                            checked_locate = _fit_short_event_budget(checked_locate, scene["remaining_seconds"], nearby_words,
                                                                    frames, scene.get("selected_moment_time", scene["start"]))
                            _check_short_moments(checked_locate, scene.get("required_moment_times", []))
                            located = {**located, "segments": checked_locate}
                        break
                    except ValueError as error:
                        if attempt == locate_attempts - 1:
                            raise
                        locate_feedback = str(error)
                        continue
                if attempt == locate_attempts - 1:
                    raise ValueError("Setup/outcome remains unresolved after bounded context expansion")
                expanded = (max(scene_bounds[0], region[0] - 30) if earlier else region[0],
                             min(scene_bounds[1], region[1] + 30) if later else region[1])
                if expanded == region:
                    raise ValueError("Needed event context is outside available source bounds")
                region = expanded
            located_parts = _long_boundary_segments(located, region, nearby_words, frames, mock=ai.mock,
                                                     final_scene=scene.get("final_scene", False), complete_combat=complete_combat)
            answer = located
            windows = None
            cold_windows = {edge: list(region) for edge in ("start", "end")}
            context = {frame["timestamp"]: frame for frame in frames}
            validation_feedback = None
            # Long scenes get one factual boundary pass (+ one repair if needed).
            # Word-safe arithmetic below replaces the expensive dense re-review.
            for precise_attempt in range(2 if short else 0):
                radius = 3 if precise_attempt == 0 else 6
                windows = [{edge: [max(region[0], part[edge] - radius), min(region[1], part[edge] + radius)] for edge in ("start", "end")}
                           for part in located_parts]
                cold_windows = None
                if is_cold:
                    if any(type(located.get(f"cold_open_{edge}")) not in (int, float)
                           or not math.isfinite(located[f"cold_open_{edge}"])
                           or not region[0] <= located[f"cold_open_{edge}"] <= region[1] for edge in ("start", "end")):
                        raise ValueError("Event-location pass needs valid cold-open boundaries")
                    cold_windows = {edge: [max(region[0], located[f"cold_open_{edge}"] - radius),
                                           min(region[1], located[f"cold_open_{edge}"] + radius)] for edge in ("start", "end")}
                # Keep event anchors as context alongside the quarter-second boundary images.
                context = {frame["timestamp"]: frame for frame in frames}
                for window in [*windows, *([cold_windows] if cold_windows else [])]:
                    for left, right in window.values():
                        for frame in sampled((left, right), 0.25):
                            context[frame["timestamp"]] = frame
                precise_key = "precise" if precise_attempt == 0 else "precise_retry"
                anchors = [part[edge] for part in located_parts for edge in ("start", "end", "action_start", "outcome_time", "reaction_end")]
                precise_frames = _bounded_frames(sorted(context.values(), key=lambda frame: frame["timestamp"]), anchors)
                context = {frame["timestamp"]: frame for frame in precise_frames}
                precise_input = _fingerprint({"frames": precise_frames,
                                              "words": nearby_words, "region": region, "windows": windows,
                                              "located": [{key: part[key] for key in ("start", "end", "action_start", "outcome_time", "reaction_end")}
                                                          for part in located_parts], "feedback": validation_feedback})
                if precise_key not in entry or (short and entry.get(f"{precise_key}_input") != precise_input):
                    entry[precise_key] = ai.refine_long_scene(
                        {**scene, "allowed_region": list(region), "boundary_pass": "precise", "located_segments": located_parts,
                         "boundary_windows": windows, "cold_open_windows": cold_windows,
                         "validation_feedback": validation_feedback}, nearby_beats, nearby_words,
                        precise_frames,
                        cold_open=is_cold)
                    if short:
                        entry[f"{precise_key}_input"] = precise_input
                    write_json(path, {**cache_key, "cuts_version": 2, "answers": answers})
                answer = entry[precise_key]
                try:
                    if short:
                        answer = _short_frame_decision(answer, sorted(context.values(), key=lambda frame: frame["timestamp"]), nearby_words)
                    checked = _long_boundary_segments(answer, region, nearby_words, list(context.values()), windows=windows, mock=ai.mock,
                                                       final_scene=scene.get("final_scene", False), complete_combat=complete_combat, constructed=short)
                    if short:
                        checked = _fit_short_event_budget(checked, scene["remaining_seconds"], nearby_words,
                                                          list(context.values()), scene.get("selected_moment_time", scene["start"]), windows)
                        _check_short_moments(checked, scene.get("required_moment_times", []))
                        answer = {**answer, "segments": checked}
                    if is_cold:
                        _long_cold_open(answer, checked, nearby_words, region, cold_windows)
                    break
                except ValueError as error:
                    if precise_attempt == 1:
                        raise
                    validation_feedback = str(error)
            correction = reviewed.get("answers", {}).get(key)
            if correction and correction.get("raw_signature") != _fingerprint(entry):
                raise ValueError("Reviewed boundary decision does not match its raw answer")
            answer = correction["decision"] if correction else answer
            parts = _long_boundary_segments(answer, region, nearby_words, list(context.values()), windows=windows, mock=ai.mock,
                                             final_scene=scene.get("final_scene", False), complete_combat=complete_combat, constructed=short)
            for part in parts:
                cuts.append({**part, "source_start": origin + part["start"], "source_end": origin + part["end"],
                             "relative_start": part["start"], "relative_end": part["end"], "scene_id": key,
                             "start_beat_id": scene["start_beat_id"], "end_beat_id": scene["end_beat_id"],
                             "purpose": scene["purpose"], "required": scene.get("required", False)})
            if is_cold:
                hook_start, hook_end = _long_cold_open(answer, parts, nearby_words, region, cold_windows)
                cold_open = {"source_start": origin + hook_start, "source_end": origin + hook_end,
                             "relative_start": hook_start, "relative_end": hook_end}
        except (ValueError, OpenAIError) as error:
            write_json(vod_dir / f"{prefix}-boundary-review.json", {**cache_key, "scene_id": key, "status": "needs_review", "reason": str(error)})
            if short:
                raise ValueError(f"{prefix} scene {key} needs editing: {error}") from error
            # A review problem lowers confidence; it must not discard the whole
            # stream. Retain the selected complete source window with safe words.
            start, end = _avoid_split_word(max(scene_bounds[0], scene["start"] - 2),
                                          min(scene_bounds[1], scene["end"] + 3), scene_words, scene_bounds)
            end = _complete_speech_exit(end, scene_words, scene_bounds)
            cuts.append({"source_start": origin + start, "source_end": origin + end,
                         "start": start, "end": end, "relative_start": start, "relative_end": end,
                         "scene_id": key, "start_beat_id": scene["start_beat_id"], "end_beat_id": scene["end_beat_id"],
                         "purpose": scene.get("purpose", "Selected source moment"), "required": scene.get("required", False),
                         "event_kind": "combat" if any(b.get("type") == "COMBAT" for b in nearby_beats) else "speech",
                         "evidence": {}, "boundary_note": str(error),
                         "start_reason": "Best available source setup", "end_reason": "Complete selected source window"})
            if is_cold:
                cold_open = {"source_start": origin + start, "source_end": origin + min(end, start + 10),
                             "relative_start": start, "relative_end": min(end, start + 10)}
    try:
        original_count = len(cuts)
        cuts = _deduplicate_long_events(cuts)
        if cold_open is None and require_intro_hint:
            raise ValueError("Missing cold open")
    except ValueError as error:
        reason = str(error)
        write_json(vod_dir / f"{prefix}-boundary-review.json", {**cache_key, "status": "needs_review", "reason": reason})
        raise
    write_json(vod_dir / f"{prefix}-boundary-review.json", {**cache_key, "status": "ready", "segment_count": len(cuts),
                                                        "notes": [{"scene_id": p["scene_id"], "reason": p["boundary_note"]}
                                                                  for p in cuts if p.get("boundary_note")],
                                                     "duplicate_event_count": original_count - len(cuts)})
    return cuts, cold_open


def _validate_long_intro(answer: dict, events: list[dict], limits: dict, words: list[dict], frames: list[dict],
                         origin: float, *, mock: bool = False) -> dict:
    if not isinstance(answer, dict) or answer.get("reject") or answer.get("complete") is not True:
        raise ValueError("Intro is not a complete verified moment")
    kind = answer.get("kind")
    if kind not in {"setup", "cold_open"} or answer.get("subject") != "elmiillor" or answer.get("related_to_selected_game") is not True:
        raise ValueError("Intro must establish this game or show ElmilloR's own successful play")
    if answer.get("outcome") != ("setup" if kind == "setup" else "success") or (kind == "cold_open" and answer.get("cool_play") is not True):
        raise ValueError("Failed plays, deaths and teammate highlights cannot be cold opens")
    supplied = {event["id"]: event for event in events}
    parts = answer.get("segments")
    if not isinstance(parts, list) or not 1 <= len(parts) <= (4 if kind == "setup" else 1):
        raise ValueError("Setup intros allow 1-4 ranges; cold opens require one complete play")
    retained = []
    previous_end = -origin
    for part in parts:
        if not isinstance(part, dict) or type(part.get("event_id")) is not int or part["event_id"] not in supplied:
            raise ValueError("Intro references an unknown event")
        event = supplied[part["event_id"]]
        if kind == "setup" and not event["setup_eligible"]:
            raise ValueError("Setup must come from relevant pregame/early-game context")
        if any(type(part.get(key)) not in (int, float) or not math.isfinite(part[key]) for key in ("start", "end")):
            raise ValueError("Intro ranges must be finite")
        start, end = _avoid_split_word(part["start"], part["end"], words, (event["start"], event["end"]))
        if not event["start"] <= start < end <= event["end"] or start < previous_end:
            raise ValueError("Intro ranges must stay inside approved events and be chronological")
        retained.append({"source_start": origin + start, "source_end": origin + end,
                         "relative_start": start, "relative_end": end})
        previous_end = end
    total = sum(part["source_end"] - part["source_start"] for part in retained)
    if not limits[kind][0] <= total <= limits[kind][1]:
        raise ValueError(f"{kind} intro duration {total:.1f}s is outside its story budget {limits[kind]}")
    evidence = answer.get("evidence", {})
    if not mock:
        if not isinstance(evidence, dict) or type(evidence.get("time")) not in (int, float) or not math.isfinite(evidence["time"]):
            raise ValueError("Intro needs a timestamped premise/success anchor")
        containing = next((part for part in retained if part["relative_start"] <= evidence["time"] < part["relative_end"]), None)
        if containing is None:
            raise ValueError("Intro evidence is outside retained footage")
        if kind == "setup":
            span = _spoken_span(str(evidence.get("quote", "")), words, containing["relative_start"], containing["relative_end"])
            if evidence.get("kind") != "speech" or span is None or abs(span[0] - evidence["time"]) > 0.5 or span[1] > containing["relative_end"]:
                raise ValueError("Setup intro must retain its exact complete premise quote")
        elif evidence.get("kind") != "visual" or len(str(evidence.get("detail", "")).strip()) < 12 or not any(
            abs(frame["timestamp"] - evidence["time"]) < 0.02 for frame in frames
        ):
            raise ValueError("A cold open needs inspected visual evidence of ElmilloR's successful play")
    return {"kind": kind, "reason": str(answer.get("reason", "")), "evidence": evidence, "segments": retained,
            "source_start": retained[0]["source_start"], "source_end": retained[-1]["source_end"]}


def _extend_intro_body(vod_dir: Path, ai: AI, plan: dict, beats: list[dict], intro: dict,
                       story_segments: list[dict], current_duration: float) -> list[dict]:
    proposals = read_json(vod_dir / "long-proposals.json")
    original = proposals.get("budget_revision", {}).get("plan", proposals["plan"])
    original_scenes = _long_scene_ranges(beats, original)
    by_range = {(scene["start_beat_id"], scene["end_beat_id"]): scene for scene in original_scenes}
    overhead = intro_duration(intro) + plan["title_duration"] + plan["end_card_duration"] - 3 * TITLE_DISSOLVE
    first_action = next(part["source_start"] for part in story_segments if part.get("event_kind") == "combat" and part.get("relative_start", 0) >= 0)
    main_segments = [part for part in story_segments if part["source_start"] >= first_action] if intro["kind"] == "setup" else story_segments
    feedback = {"duration": current_duration, "current_main_duration": sum(part["source_end"] - part["source_start"] for part in main_segments),
                "footage_budget": [540 - overhead, 720 - overhead],
                "required_ranges": list(by_range), "cold_open_beat_id": original.get("cold_open_beat_id"),
                "approved_segments": main_segments, "approved_intro": intro,
                "excluded_preparation_ranges": [[part["source_start"], part["source_end"]] for part in story_segments if part not in main_segments],
                "reason": "The concise setup intro REPLACES pre-action preparation: do NOT count excluded preparation toward the footage budget. Add at least ONE new optional complete event to the MAIN edit, never pad the intro. Preserve all existing scene ranges."}
    path = vod_dir / "long-intro-budget.json"
    signature = _fingerprint(feedback)
    revision = read_json(path) if path.exists() else {}
    if revision.get("signature") != signature:
        revision = {"signature": signature, "plan": ai.plan_long(beats, budget_feedback=feedback)}
        write_json(path, revision)
    scenes = _long_scene_ranges(beats, revision["plan"])
    if not by_range.keys() <= {(scene["start_beat_id"], scene["end_beat_id"]) for scene in scenes}:
        raise ValueError("Intro-budget revision removed an approved story unit")
    scenes = [scene for scene in scenes if (scene["start_beat_id"], scene["end_beat_id"]) not in by_range]
    if not scenes:
        raise ValueError("Intro-budget revision did not add any new main-video event")
    raw_cuts = read_json(vod_dir / "long-cuts.json")
    cache_key = {key: value for key, value in raw_cuts.items() if key not in {"answers", "cuts_version"}}
    game = selected_game(vod_dir)
    cuts, _hint = _refine_long_scenes(vod_dir, ai, game, beats, scenes, cache_key, None, False, require_intro_hint=False)
    return _deduplicate_long_events([*story_segments, *cuts])


def _apply_long_intro(vod_dir: Path, ai: AI, plan: dict, beats: list[dict], force: bool = False) -> dict:
    origin = plan["gameplay_start"]
    source = Path(read_json(vod_dir / "manifest.json")["source"])
    story_segments = plan.get("story_segments", plan["segments"])
    first_action = next((part["source_start"] for part in story_segments
                         if part.get("event_kind") == "combat" and part.get("relative_start", 0) >= 0), None)
    after_setup = [part for part in story_segments if first_action is not None and part["source_start"] >= first_action]
    overhead = plan["title_duration"] + plan["end_card_duration"] - 3 * TITLE_DISSOLVE
    setup_base = overhead + sum(part["source_end"] - part["source_start"] for part in after_setup)
    cold_base = overhead + sum(part["source_end"] - part["source_start"] for part in story_segments)
    limits = {"setup": [8, min(40, 720 - setup_base)],
              "cold_open": [max(5, 540 - cold_base), min(20, 720 - cold_base)]}
    events = []
    for index, part in enumerate(story_segments):
        start, end = part["source_start"] - origin, part["source_end"] - origin
        events.append({"id": index, "start": start, "end": end, "event_kind": part.get("event_kind", "combat"),
                       "purpose": part.get("purpose", ""), "result_evidence": part.get("evidence", {}),
                       "setup_eligible": first_action is not None and part["source_end"] <= first_action,
                       "text": " ".join(beat.get("text", "") for beat in beats if beat["start"] < end and beat["end"] > start)})
    pregame = [beat for beat in beats if beat.get("phase") == "CHAMP_SELECT" and beat.get("text")]
    if pregame:
        events.append({"id": len(events), "start": max(-origin, pregame[0]["start"]), "end": 0.0,
                       "event_kind": "speech", "setup_eligible": True, "purpose": "Relevant champion-select premise only",
                       "text": " ".join(beat["text"] for beat in pregame)})
    words = transcript_words(vod_dir)
    pregame_path = vod_dir / "pregame-transcript.json"
    if pregame_path.exists():
        transcript = read_json(pregame_path)
        offset = transcript.get("audio_start", origin) - origin
        words = sorted([*words, *[{**word, "start": word["start"] + offset, "end": word["end"] + offset}
                                 for word in transcript.get("words", [])]], key=lambda word: word["start"])
    inputs = _fingerprint({"events": events, "story": plan.get("story", ""), "words": words, "limits": limits,
                           "source": _source_identity(source), "model": ai.editorial_model, "mock": ai.mock})
    path = vod_dir / "long-intro.json"
    cache = read_json(path) if path.exists() and not force else {}
    if not _cache_matches(cache, intro_version=1, inputs=inputs):
        cache = {"intro_version": 1, "inputs": inputs, "attempts": []}
    feedback = None
    for attempt in range(2):
        if len(cache["attempts"]) <= attempt:
            cache["attempts"].append({"proposal": ai.propose_long_intro(events, plan.get("story", ""), limits, feedback=feedback)})
            write_json(path, cache)
        raw = cache["attempts"][attempt]
        try:
            proposal = raw["proposal"]
            ids = proposal.get("event_ids", [])
            if (proposal.get("kind") not in limits or not isinstance(ids, list) or not 1 <= len(ids) <= 4
                    or any(type(index) is not int or not 0 <= index < len(events) for index in ids) or len(set(ids)) != len(ids)
                    or (attempt == 1 and proposal["kind"] != "setup")):
                raise ValueError("Invalid intro proposal; use relevant setup when a successful play cannot be verified")
            selected = sorted([events[index] for index in ids], key=lambda event: event["start"])
            frames = {}
            for event in selected:
                regions = [(event["start"], event["end"], 2),
                           (event["start"], min(event["end"], event["start"] + 6), 0.25),
                           (max(event["start"], event["end"] - 6), event["end"], 0.25)]
                for left, right, interval in regions:
                    tag = _fingerprint({"source": inputs, "start": left, "end": right, "interval": interval})
                    for frame in sample_boundary_frames(source, vod_dir / "frames" / "intro" / tag, interval,
                                                        start=origin + left, end=origin + right):
                        time = frame["timestamp"] - origin
                        frames[time] = {**frame, "timestamp": time}
            frames = {frame["timestamp"]: frame for frame in _bounded_frames(sorted(frames.values(), key=lambda frame: frame["timestamp"]))}
            nearby_words = [word for word in words if any(event["start"] <= word["start"] < event["end"] for event in selected)]
            if "refinement" not in raw:
                raw["refinement"] = ai.refine_long_intro(proposal, selected, nearby_words, sorted(frames.values(), key=lambda frame: frame["timestamp"]), limits)
                write_json(path, cache)
            intro = _validate_long_intro(raw["refinement"], selected, limits, nearby_words, list(frames.values()), origin, mock=ai.mock)
            if intro["kind"] != proposal["kind"]:
                raise ValueError("Intro refinement changed its proposed kind")
            body = after_setup if intro["kind"] == "setup" else story_segments
            if not body or (intro["kind"] == "setup" and intro["source_end"] > body[0]["source_start"]):
                raise ValueError("Setup intro must finish before the main edit's pre-action entry")
            duration_seconds = intro_duration(intro) + overhead + sum(part["source_end"] - part["source_start"] for part in body)
            if duration_seconds < 540:
                story_segments = _extend_intro_body(vod_dir, ai, plan, beats, intro, story_segments, duration_seconds)
                body = [part for part in story_segments if part["source_start"] >= first_action] if intro["kind"] == "setup" else story_segments
                duration_seconds = intro_duration(intro) + overhead + sum(part["source_end"] - part["source_start"] for part in body)
            if not 540 <= duration_seconds <= 720:
                raise ValueError(f"Video duration {duration_seconds:.1f}s is outside 9-12 minutes after intro selection")
            selected_ids = [beat["id"] for beat in beats if any(beat["start"] < part["source_end"] - origin
                            and beat["end"] > part["source_start"] - origin for part in [*intro["segments"], *body])]
            return {**plan, "long_version": 16, "intro_policy_version": 1, "intro": intro, "cold_open": intro,
                    "story_segments": story_segments, "segments": body, "title_transition": TITLE_DISSOLVE,
                    "duration": duration_seconds, "selected_ids": selected_ids,
                    "gameplay_duration": sum(part["source_end"] - part["source_start"] for part in body)}
        except ValueError as error:
            feedback = str(error)
            raw["validation_error"] = feedback
            write_json(path, cache)
    raise ValueError(f"Intro needs review after one setup retry: {feedback}")


def _local_long_intro(plan: dict, words: list[dict]) -> dict:
    """Keep the opening premise once, without another model or duration rebuild."""
    body = [dict(part) for part in plan["segments"]]
    first = body.pop(0)
    origin = plan["gameplay_start"]
    start = first["source_start"] - origin
    end = first["source_end"] - origin
    if end - start > 40:
        pauses = [float(left["end"]) + 0.1 for left, right in zip(words, words[1:])
                  if 8 <= float(left["end"]) - start <= 40 and float(right["start"]) - float(left["end"]) >= 0.45]
        if pauses:
            end = min(pauses, key=lambda time: abs(time - start - 25))
            body.insert(0, {**first, "source_start": origin + end, "relative_start": end, "start": end})
        elif body and len(words) > 1:
            # A complete approved opening is better than a card mid-sentence.
            pass
        else:
            safe_ends = [float(word["end"]) for word in words if 8 <= float(word["end"]) - start <= 30]
            end = max(safe_ends, default=start + 25)
            body.insert(0, {**first, "source_start": origin + end, "relative_start": end, "start": end})
    intro = {"kind": "setup", "reason": "Complete opening premise, retained once from the selected story",
             "source_start": origin + start, "source_end": origin + end,
             "relative_start": start, "relative_end": end}
    intro["segments"] = [{key: value for key, value in intro.items() if key in {
        "source_start", "source_end", "relative_start", "relative_end"}}]
    if not body:
        raise ValueError("Long story needs footage after its introduction")
    duration = (end - start + plan["title_duration"] + plan["end_card_duration"] - 3 * TITLE_DISSOLVE
                + sum(part["source_end"] - part["source_start"] for part in body))
    return {**plan, "long_version": 16, "intro_policy_version": 1, "intro": intro, "cold_open": intro,
            "story_segments": plan["segments"], "segments": body, "duration": duration,
            "gameplay_duration": sum(part["source_end"] - part["source_start"] for part in body)}


def plan_long(vod_dir: Path, ai: AI, force: bool = False) -> dict:
    output = vod_dir / "long-plan.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    identity = {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end}
    manifest = read_json(vod_dir / "manifest.json")
    cache_key = {**identity, "beats": _file_fingerprint(vod_dir / "beats.json"),
                 "transcripts": {name: _file_fingerprint(vod_dir / name) for name in ("transcript.json", "pregame-transcript.json")},
                 "transcript_edits": _transcript_edits(vod_dir), "source_identity": _source_identity(Path(manifest["source"])),
                 "editorial_model": getattr(ai, "editorial_model", "mock"), "mock": ai.mock, "boundary_version": 3}
    review_signature = _file_fingerprint(vod_dir / "long-cuts.reviewed.json")
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key, review_signature=review_signature) and cached.get("long_version") in (15, 16):
            if cached.get("intro_policy_version") != 1:
                cached = _local_long_intro(cached, transcript_words(vod_dir))
                write_json(output, cached)
            return cached
    beats = _selected_beats(vod_dir, identity)
    proposal_path = vod_dir / "long-proposals.json"
    cached_proposal = read_json(proposal_path) if proposal_path.exists() else {}
    if not force and _cache_matches(cached_proposal, **cache_key, proposal_version=2):
        model_plan = cached_proposal["plan"]
    else:
        model_plan = ai.plan_long(beats)
        cached_proposal = {**cache_key, "proposal_version": 2, "plan": model_plan}
        write_json(proposal_path, cached_proposal)
    scenes = _long_scene_ranges(beats, model_plan)
    if not force and cached_proposal.get("budget_revision", {}).get("plan"):
        original = {(scene["start_beat_id"], scene["end_beat_id"]): scene for scene in scenes}
        revised = cached_proposal["budget_revision"]["plan"]
        revised_scenes = _long_scene_ranges(beats, revised)
        required = {key for key, scene in original.items() if scene.get("required") or (
            type(model_plan.get("cold_open_beat_id")) is int and scene["start_beat_id"] <= model_plan["cold_open_beat_id"] <= scene["end_beat_id"])}
        if not required <= {(scene["start_beat_id"], scene["end_beat_id"]) for scene in revised_scenes} or revised.get("cold_open_beat_id") != model_plan.get("cold_open_beat_id"):
            raise ValueError("Budget revision needs review: it removed required premise/ending or changed the cold open")
        model_plan = revised
        scenes = [original.get((scene["start_beat_id"], scene["end_beat_id"]), scene)
                  for scene in revised_scenes]
    title_duration, end_card_duration = TITLE_DURATION, 12.0
    for attempt in range(2):
        segments, cold_open = _refine_long_scenes(
            vod_dir, ai, game, beats, scenes, cache_key, None, force and attempt == 0
        )
        gameplay_duration = sum(segment["source_end"] - segment["source_start"] for segment in segments)
        overhead = title_duration + end_card_duration - 3 * TITLE_DISSOLVE
        duration = gameplay_duration + overhead
        if 540 <= duration <= 720 or attempt == 1:
            break
        required = {(scene["start_beat_id"], scene["end_beat_id"]): scene for scene in scenes if scene.get("required") or (
            type(model_plan.get("cold_open_beat_id")) is int and scene["start_beat_id"] <= model_plan["cold_open_beat_id"] <= scene["end_beat_id"])}
        feedback = {"duration": duration, "footage_budget": [600 - overhead, 720 - overhead],
                    "required_ranges": list(required), "cold_open_beat_id": model_plan.get("cold_open_beat_id"), "approved_segments": segments,
                    "reason": "Add relevant complete moments" if duration < 540 else "Remove optional complete story units"}
        signature = _fingerprint(feedback)
        revision = cached_proposal.get("budget_revision", {})
        if revision.get("signature") != signature:
            revision = {"signature": signature, "plan": ai.plan_long(beats, budget_feedback=feedback)}
            cached_proposal["budget_revision"] = revision
            write_json(proposal_path, cached_proposal)
        revised = revision["plan"]
        next_scenes = _long_scene_ranges(beats, revised)
        ranges = {(scene["start_beat_id"], scene["end_beat_id"]) for scene in next_scenes}
        if not required.keys() <= ranges or revised.get("cold_open_beat_id") != model_plan.get("cold_open_beat_id"):
            reason = "Budget revision needs review: it removed required premise/ending or changed the cold open"
            write_json(vod_dir / "long-boundary-review.json", {**cache_key, "status": "needs_review", "reason": reason})
            raise ValueError(reason)
        # Reuse retained scenes exactly: a budget pass cannot change approved boundaries or purpose.
        original = {(scene["start_beat_id"], scene["end_beat_id"]): scene for scene in scenes}
        scenes = [original.get((scene["start_beat_id"], scene["end_beat_id"]), scene) for scene in next_scenes]
        model_plan = revised
    if not 0 < duration <= 720:
        reason = f"Long edit duration {duration:.1f}s exceeds the available duration budget"
        write_json(vod_dir / "long-boundary-review.json", {**cache_key, "status": "needs_review", "reason": reason})
        raise ValueError(reason)
    selected = [beat["id"] for beat in beats if any(beat["start"] < segment.get("relative_end", segment["source_end"] - game["gameplay_start"])
                and beat["end"] > segment.get("relative_start", segment["source_start"] - game["gameplay_start"]) for segment in segments)]
    plan = {
        **cache_key, "long_version": 15, "review_signature": review_signature,
        "story": model_plan.get("story", ""),
        "selected_ids_raw": model_plan.get("selected_ids", []),
        "selected_ids": selected,
        "gameplay_duration": gameplay_duration,
        "duration": duration,
        "cold_open": cold_open or segments[0],
        "title_duration": title_duration,
        "end_card_duration": end_card_duration,
        "segments": segments,
    }
    # The opening comes from the approved story. No separate intro proposal,
    # dense review, or third global duration revision is needed.
    plan = _local_long_intro(plan, transcript_words(vod_dir) if (vod_dir / "transcript.json").exists() else [])
    write_json(output, plan)
    return plan


def _download_asset(path: Path, url: str) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 milloclips/0.1"})
        with urlopen(request, timeout=30) as response:
            path.write_bytes(response.read())
    return path


def _ease(value: float) -> float:
    value = min(1, max(0, value))
    return value * value * (3 - 2 * value)


def _audio_spectrum(path: Path, start: float, duration_seconds: float = 5, fps: int = 30) -> list[list[float]]:
    with tempfile.TemporaryDirectory() as temporary:
        audio = Path(temporary) / "title.wav"
        run_ffmpeg(["-ss", str(start), "-t", str(duration_seconds), "-i", str(path), "-vn", "-ac", "1", "-ar", "3000",
                    "-c:a", "pcm_s16le", str(audio)])
        with wave.open(str(audio), "rb") as source:
            sample_rate = source.getframerate()
            raw = source.readframes(source.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    frame_size = round(sample_rate / fps)
    window = [0.5 - 0.5 * math.cos(2 * math.pi * index / (frame_size - 1)) for index in range(frame_size)]
    basis = [
        [(math.cos(2 * math.pi * frequency * index / frame_size) * window[index],
          math.sin(2 * math.pi * frequency * index / frame_size) * window[index]) for index in range(frame_size)]
        for frequency in range(2, 50)
    ]
    spectra = []
    for index in range(round(duration_seconds * fps)):
        chunk = samples[index * frame_size:(index + 1) * frame_size]
        chunk = (*chunk, *((0,) * (frame_size - len(chunk))))
        spectra.append([
            math.hypot(sum(sample * cosine for sample, (cosine, _) in zip(chunk, wave_basis)),
                       sum(sample * sine for sample, (_, sine) in zip(chunk, wave_basis)))
            for wave_basis in basis
        ])
    bounds = []
    for band in zip(*spectra):
        ordered = sorted(band)
        bounds.append((ordered[len(ordered) // 10], ordered[round(len(ordered) * 0.95) - 1]))
    result = []
    for spectrum in spectra:
        # Spatial smoothing only: temporal averaging would delay the visible beat.
        levels = [min(1, max(0, (value - floor) / max(0.001, peak - floor))) ** 1.4
                  for value, (floor, peak) in zip(spectrum, bounds)]
        result.append([
            (levels[max(0, index - 1)] + 2 * value + levels[min(len(levels) - 1, index + 1)]) / 4
            for index, value in enumerate(levels)
        ])
    return result


def _title_frame(logo: Image.Image, time: float, spectrum: list[float] | None = None,
                 duration_seconds: float = TITLE_DURATION) -> Image.Image:
    background = "#060b18"
    frame = Image.new("RGBA", (1280, 720), background)
    entrance = _ease(min(time, duration_seconds - time))
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * max(0, time - 1) / 2.1)
    scale = 0.94 + 0.06 * entrance + 0.08 * breath
    supersample = 4
    icon = ImageOps.contain(logo, (280 * supersample, 280 * supersample), Image.Resampling.LANCZOS)
    icon = icon.resize((round(icon.width * scale), round(icon.height * scale)), Image.Resampling.LANCZOS)
    icon_layer = Image.new("RGBA", (340 * supersample, 340 * supersample))
    icon_layer.alpha_composite(icon, ((icon_layer.width - icon.width) // 2, (icon_layer.height - icon.height) // 2))
    icon_layer = icon_layer.resize((340, 340), Image.Resampling.LANCZOS)
    icon_layer.putalpha(icon_layer.getchannel("A").point(lambda alpha: round(alpha * entrance)))
    glow = Image.new("RGBA", frame.size)
    glow_expansion = round(18 * breath)
    ImageDraw.Draw(glow).ellipse((390 - glow_expansion, 110 - glow_expansion, 890 + glow_expansion, 610 + glow_expansion),
                                  fill=(*ACCENT, round((40 + 8 * breath) * entrance)))
    frame = Image.alpha_composite(frame, glow.filter(ImageFilter.GaussianBlur(round(110 + 8 * breath))))
    spectrum = spectrum or [0.0] * 48
    energy = sum(spectrum) / len(spectrum)
    ring_scale, ring_size = 4, 680
    ring_points = []
    for index in range(192):
        angle = math.pi / 2 + 2 * math.pi * index / 192
        band_position = (index if index < 96 else 191 - index) * 47 / 95
        band = min(46, int(band_position))
        fraction = band_position - band
        level = spectrum[band] * (1 - fraction) + spectrum[band + 1] * fraction
        vibration = (90 * level + 18 * energy) * entrance
        radius = 200 + vibration
        ring_points.append(((ring_size / 2 + math.cos(angle) * radius) * ring_scale,
                            (ring_size / 2 + math.sin(angle) * radius) * ring_scale))
    ring_points.append(ring_points[0])
    ring = Image.new("RGBA", (ring_size * ring_scale, ring_size * ring_scale))
    ring_glow = Image.new("RGBA", ring.size)
    ImageDraw.Draw(ring_glow).line(ring_points, fill=(*ACCENT, round((45 + 90 * energy) * entrance)),
                                   width=12 * ring_scale, joint="curve")
    ring = Image.alpha_composite(ring, ring_glow.filter(ImageFilter.GaussianBlur(3 * ring_scale)))
    ImageDraw.Draw(ring).line(ring_points, fill=(*ACCENT, round((180 + 75 * energy) * entrance)),
                              width=round((3 + 2 * energy) * ring_scale), joint="curve")
    ring = ring.resize((ring_size, ring_size), Image.Resampling.LANCZOS)
    frame.alpha_composite(ring, ((1280 - ring_size) // 2, (720 - ring_size) // 2))
    frame.alpha_composite(icon_layer, ((1280 - 340) // 2, (720 - 340) // 2))
    return frame.convert("RGB")


def _render_title_card(path: Path, icon: Path, music: Path, duration_seconds: float = TITLE_DURATION) -> None:
    with Image.open(icon) as source, tempfile.TemporaryDirectory() as temporary:
        logo = source.convert("RGBA")
        frames = Path(temporary)
        spectra = _audio_spectrum(music, TITLE_MUSIC_START + TITLE_MUSIC_LEAD, duration_seconds)
        for index, spectrum in enumerate(spectra):
            _title_frame(logo, index / 30, spectrum, duration_seconds).save(frames / f"frame-{index + 1:03d}.png")
        run_ffmpeg([
            "-framerate", "30", "-i", str(frames / "frame-%03d.png"),
            "-f", "lavfi", "-t", str(duration_seconds), "-i", "anullsrc=r=48000:cl=stereo",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path),
        ])


def _trailer(shared: Path) -> Path:
    cached = sorted(shared.glob("fiddlesticks-trailer.*"))
    if cached:
        return cached[0]
    with YoutubeDL({
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg(),
        "outtmpl": str(shared / "fiddlesticks-trailer.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
    }) as downloader:
        downloader.download(["https://www.youtube.com/watch?v=jNn2F39G-6s"])
    cached = sorted(shared.glob("fiddlesticks-trailer.*"))
    if not cached:
        raise RuntimeError("yt-dlp finished without creating the Fiddlesticks trailer asset")
    return cached[0]


def _dream_eater(shared: Path) -> Path:
    cached = [path for path in shared.glob("dream-eater-source.*") if path.suffix in {".m4a", ".mp3", ".opus", ".webm"}]
    if cached:
        return cached[0]
    with YoutubeDL({
        "format": "bestaudio/best",
        "ffmpeg_location": ffmpeg(),
        "outtmpl": str(shared / "dream-eater-source.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
    }) as downloader:
        downloader.download(["https://www.youtube.com/watch?v=FDMYwUn5CIg"])
    cached = [path for path in shared.glob("dream-eater-source.*") if path.suffix in {".m4a", ".mp3", ".opus", ".webm"}]
    if not cached:
        raise RuntimeError("yt-dlp finished without creating the Dream Eater audio asset")
    return cached[0]


def _outro_overlay(
    time: float,
    title_font: ImageFont.FreeTypeFont,
    info_font: ImageFont.FreeTypeFont,
    kick_icon: Image.Image,
    x_icon: Image.Image,
) -> Image.Image:
    overlay = Image.new("RGBA", (1280, 720))
    opacity = 1 if time < 10 else _ease((12 - time) / 2)
    if opacity <= 0:
        return overlay
    alpha = round(255 * opacity)
    draw = ImageDraw.Draw(overlay)
    label = "LIKE Y SUSCRIBETE"
    box = draw.textbbox((0, 0), label, font=title_font, stroke_width=3)
    draw.text(((1280 - box[2]) / 2, 74), label, font=title_font, fill=(*ACCENT, alpha), stroke_width=3,
              stroke_fill=(5, 7, 6, alpha))
    follow = "SIGUEME EN KICK Y TWITTER"
    box = draw.textbbox((0, 0), follow, font=info_font, stroke_width=3)
    draw.text(((1280 - box[2]) / 2, 500), follow, font=info_font, fill=(*ACCENT, alpha), stroke_width=3,
              stroke_fill=(5, 7, 6, alpha))

    # Keep the center and lower frame empty for YouTube's channel badge and recommended-video elements.
    icon_y = 590
    for icon, icon_x, text, text_x in (
        (kick_icon, 165, "KICK.COM/ELMIILLOR", 225),
        (x_icon, 795, "@ELMIILLOR", 855),
    ):
        visible = icon.copy()
        visible.putalpha(visible.getchannel("A").point(lambda value: round(value * opacity)))
        overlay.alpha_composite(visible, (icon_x, icon_y))
        text_box = draw.textbbox((0, 0), text, font=info_font, stroke_width=3)
        text_y = icon_y + (46 - (text_box[3] - text_box[1])) / 2 - text_box[1]
        draw.text((text_x, text_y), text, font=info_font, fill=(255, 255, 255, alpha), stroke_width=3,
                  stroke_fill=(5, 7, 6, alpha))
    return overlay


def _render_end_card(path: Path, trailer: Path, kick: Path, x: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        frames = Path(temporary)
        title_font = ImageFont.truetype(FILE, 68)
        info_font = ImageFont.truetype(FILE, 39)
        with Image.open(kick) as source:
            kick_icon = ImageOps.contain(source.convert("RGBA"), (46, 46), Image.Resampling.LANCZOS)
        with Image.open(x) as source:
            x_icon = ImageOps.contain(source.convert("RGBA"), (46, 46), Image.Resampling.LANCZOS)
        for index in range(360):
            _outro_overlay(index / 30, title_font, info_font, kick_icon, x_icon).save(frames / f"frame-{index + 1:03d}.png")
        video = (
            "[0:v]trim=duration=12,setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=increase,"
            "crop=1280:720,fps=30,eq=contrast=1.08:brightness=-0.08[base];"
            "[1:v]format=rgba[overlay];[base][overlay]overlay=shortest=1:format=auto[v]"
        )
        run_ffmpeg([
            "-ss", "53", "-t", "12", "-i", str(trailer), "-framerate", "30", "-i", str(frames / "frame-%03d.png"),
            "-f", "lavfi", "-t", "12", "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex", video,
            "-map", "[v]", "-map", "2:a",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path),
        ])


def _long_cards(title_duration: float = TITLE_DURATION) -> tuple[Path, Path, Path]:
    shared = Path("assets") / "long"
    icon = _download_asset(shared / "elmiillor-icon.png", "https://elmiillor.com/favicon.png")
    kick = _download_asset(shared / "kick-icon.ico", "https://kick.com/favicon.ico")
    x = _download_asset(shared / "x-icon.ico", "https://x.com/favicon.ico")
    music = _dream_eater(shared)
    source = shared / "dream-eater-source.json"
    source_metadata = {"name": "Dream Eater", "artist": "Hyleo", "collection": "Sessions: Star Guardian Taliyah",
                       "source": "https://www.youtube.com/watch?v=FDMYwUn5CIg", "title_clip_start": TITLE_MUSIC_START,
                       "end_clip_start": 42, "end_card_sync": 43}
    if not source.exists() or read_json(source) != source_metadata:
        write_json(source, source_metadata)
    title_path, end_path = shared / "title-card.mp4", shared / "end-card.mp4"
    title_metadata = shared / "title-card.json"
    title_key = {"version": 4, "icon": _file_fingerprint(icon), "music": _source_identity(music), "duration": title_duration,
                 "spectrum_start": TITLE_MUSIC_START + TITLE_MUSIC_LEAD}
    if not title_path.exists() or not title_metadata.exists() or read_json(title_metadata) != title_key or abs(duration(title_path) - title_duration) > 0.1:
        _render_title_card(title_path, icon, music, title_duration)
        write_json(title_metadata, title_key)
    if not end_path.exists() or abs(duration(end_path) - 12) > 0.1:
        _render_end_card(end_path, _trailer(shared), kick, x)
    return title_path, end_path, music


def render_long(vod_dir: Path) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    plan = read_json(vod_dir / "long-plan.json")
    output = vod_dir / "renders" / "long-rough.mp4"
    if plan.get("long_version") != 16 or plan.get("intro_policy_version") != 1:
        raise SystemExit("long-plan.json predates setup/successful-play intros; run plan-long first")
    plan["title_duration"] = TITLE_DURATION
    title, end, music = _long_cards(plan["title_duration"])
    intro_seconds = intro_duration(plan["cold_open"])
    plan["title_transition"] = TITLE_DISSOLVE
    plan["duration"] = (title_start(intro_seconds, plan["title_duration"]) + plan["title_duration"] - 2 * TITLE_DISSOLVE
                        + plan["end_card_duration"] + sum(part["source_end"] - part["source_start"] for part in plan["segments"]))
    write_json(vod_dir / "long-plan.json", plan)
    source = Path(manifest["source"])
    sidecar_path = output.with_suffix(".json")
    provenance = {"render_version": 5, "plan": _fingerprint(plan), "cards": [_media_fingerprint(card) for card in (title, end)],
                  "source": _source_identity(source)}
    # Resuming a batch must not re-run ffmpeg when the render still matches its plan.
    if output.exists() and sidecar_path.exists():
        sidecar = read_json(sidecar_path)
        if all(sidecar.get(field) == value for field, value in provenance.items()) \
                and sidecar.get("output") == _media_fingerprint(output) \
                and abs(float(sidecar.get("planned_duration", -1)) - plan["duration"]) <= 0.01 \
                and sidecar.get("width") == 1280 and sidecar.get("height") == 720 and sidecar.get("audio") is True:
            return output
    pending = output.with_name("long-rough.pending.mp4")
    render_long_video(source, plan["segments"], plan["cold_open"], title, end, pending,
                       title_duration=plan["title_duration"], end_duration=plan["end_card_duration"],
                       title_transition=plan["title_transition"],
                       title_music=music, title_music_start=TITLE_MUSIC_START, end_music=music, end_music_start=42)
    info = media_info(pending)
    if abs(info["duration"] - plan["duration"]) > 0.5 or not info["audio"] or (info["width"], info["height"]) != (1280, 720):
        raise ValueError("Long render does not match its precise edit plan")
    pending.replace(output)
    write_json(sidecar_path, {**provenance, "output": _media_fingerprint(output),
                              "planned_duration": plan["duration"], **info})
    render_long_review(vod_dir)
    return output


def _long_transitions(plan: dict) -> list[dict]:
    intro_parts = plan["cold_open"].get("segments") or [plan["cold_open"]]
    transitions = []
    cursor = 0
    for index, part in enumerate(intro_parts[:-1]):
        cursor += part["source_end"] - part["source_start"]
        transitions.append({"output_time": cursor, "label": f"intro {index + 1} -> intro {index + 2}",
                            "outgoing_source_end": part["source_end"], "incoming_source_start": intro_parts[index + 1]["source_start"]})
    cursor = title_start(intro_duration(plan["cold_open"]), plan["title_duration"],
                         plan.get("title_transition", TITLE_DISSOLVE))
    transitions.append({"output_time": cursor, "label": "intro -> icon card", "dissolve_seconds": plan.get("title_transition", TITLE_DISSOLVE)})
    cursor += plan["title_duration"] - plan.get("title_transition", TITLE_DISSOLVE)
    transitions.append({"output_time": cursor, "label": "channel title -> premise",
                        "dissolve_seconds": plan.get("title_transition", TITLE_DISSOLVE)})
    for index, segment in enumerate(plan["segments"]):
        cursor += segment["source_end"] - segment["source_start"]
        label = "result -> end card" if index == len(plan["segments"]) - 1 else f"scene {index + 1} -> scene {index + 2}"
        if index == len(plan["segments"]) - 1:
            cursor -= plan.get("title_transition", TITLE_DISSOLVE)
        next_segment = plan["segments"][index + 1] if index + 1 < len(plan["segments"]) else None
        transitions.append({"output_time": cursor, "label": label,
                            "dissolve_seconds": plan.get("title_transition", TITLE_DISSOLVE) if next_segment is None else 0,
                            "outgoing_source_end": segment["source_end"], "exit_reason": segment.get("end_reason", ""),
                            "incoming_source_start": next_segment["source_start"] if next_segment else None,
                            "entry_reason": next_segment.get("start_reason", "") if next_segment else "Reusable end card",
                            "scene_id": segment.get("scene_id")})
        if segment.get("reaction_transition"):
            transitions[-1]["reaction_transition"] = segment["reaction_transition"]
            if next_segment is not None:
                transitions[-1]["dissolve_seconds"] = 4 / 30
    return transitions


def render_long_review(vod_dir: Path) -> Path:
    plan = read_json(vod_dir / "long-plan.json")
    source = vod_dir / "renders" / "long-rough.mp4"
    if not source.exists():
        raise SystemExit("Render the long video before creating its transition review")
    transitions = _long_transitions(plan)
    write_json(vod_dir / "long-review.json", {"duration": plan["duration"], "window_seconds": 8, "transitions": transitions})
    intervals = [
        {"source_start": max(0, item["output_time"] - 4), "source_end": min(plan["duration"], item["output_time"] + 4)}
        for item in transitions
    ]
    output = vod_dir / "renders" / "long-transitions.mp4"
    render_segments(source, intervals, output)
    return output


def _spoken_span(quote: str, words: list[dict], start: float, end: float, *, minimum_tokens: int = 2) -> tuple[float, float] | None:
    def tokens(text: str) -> list[str]:
        plain = "".join(letter for letter in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(letter))
        return re.findall(r"\w+", plain)

    phrase = tokens(quote)
    if len(phrase) < minimum_tokens:
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
        return (type(before) in (int, float) and math.isfinite(before) and start <= before < time
                and len(str(evidence.get("detail", "")).strip()) >= 12
                and all(any(abs(frame["timestamp"] - moment) <= 1 for frame in frames) for moment in (before, time)))
    if evidence.get("kind") != "speech":
        return False
    span = _spoken_span(str(evidence.get("quote", "")), words, start, end)
    return span is not None and span[1] <= end and abs(span[0] - time) <= 0.5


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


def _finish_short_range(start: float, end: float, words: list[dict], region: tuple[float, float], required_end: float) -> tuple[float, float]:
    """Construct a speech-safe cut, retaining its evidenced premise or outcome."""
    if required_end > region[1]:
        raise ValueError("Inspected context does not contain the complete evidenced phrase")
    end = max(end, required_end)
    try:
        start, end = _avoid_split_word(start, end, words, region)
        end = _complete_speech_exit(end, words, region)
    except ValueError:
        # A proposed exit at the context edge may split speech. Choose a real pause
        # within inspected footage instead of discarding this selected moment.
        pauses = [float(a["end"]) + 0.1 for a, b in zip(words, words[1:])
                  if max(start + 1, required_end) <= float(a["end"]) <= region[1] - 0.1
                  and float(b["start"]) - float(a["end"]) >= 0.45]
        if not pauses:
            raise ValueError("More context is needed to finish dialogue after the evidenced event")
        end = min(pauses, key=lambda pause: abs(pause - end))
        start, end = _avoid_split_word(start, end, words, region)
        end = _complete_speech_exit(end, words, region)
    if not region[0] <= start < end <= region[1] or end < required_end:
        raise ValueError("Speech-safe cut exceeds inspected context or loses its evidenced event")
    return start, end


def _retime_short(candidate: dict, answer: dict, words: list[dict], frames: list[dict], game_start: float, region: tuple[float, float]) -> dict:
    if not isinstance(answer, dict) or answer.get("reject"):
        raise ValueError("Editor did not provide an edit; inspect more context for the same selected story")
    if not all(isinstance(answer.get(key), str) and answer[key].strip() for key in ("title", "story", "hook", "payoff")):
        raise ValueError("Editor must retain title, story, hook and payoff")
    segments = answer.get("segments")
    first = segments[0] if isinstance(segments, list) and segments and isinstance(segments[0], dict) else {}
    last = segments[-1] if isinstance(segments, list) and segments and isinstance(segments[-1], dict) else {}
    hook = _resolve_evidence(answer.get("hook_evidence") or first.get("hook_evidence"), words, frames, *region)
    payoff = _resolve_evidence(answer.get("payoff_evidence") or last.get("payoff_evidence"), words, frames, *region, prefer_last=True)
    if not hook or not payoff:
        raise ValueError("Hook or payoff evidence is missing from inspected context")
    if answer.get("event_evidence"):
        event = _resolve_evidence(answer["event_evidence"], words, frames, *region)
        if not event or not hook[0]["time"] < event[0]["time"] < payoff[0]["time"]:
            raise ValueError("Event evidence is missing or does not connect hook and payoff")
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
    start, end = _finish_short_range(start, min(region[1], end), words, region, payoff[1])
    if not region[0] <= start < end <= region[1] or not 8 <= end - start <= SHORT_MAX_SECONDS:
        raise ValueError(f"Edit must retain a complete 8-{SHORT_MAX_SECONDS} second story within inspected context")
    segment = {"relative_start": start, "relative_end": end, "source_start": game_start + start, "source_end": game_start + end}
    if not _short_evidence(hook[0], segment, words, frames, hook=True) or not _short_evidence(payoff[0], segment, words, frames, hook=False):
        raise ValueError("Constructed cut does not contain its complete hook and payoff evidence")
    return {
        **candidate, "segments": [segment], **segment, "duration": end - start,
        "title": answer["title"][:100], "reason": answer["story"][:240],
        "hook": answer["hook"][:240], "payoff": answer["payoff"][:240],
        "hook_evidence": hook[0], "payoff_evidence": payoff[0],
        **({"event_evidence": event[0]} if answer.get("event_evidence") else {}),
    }


def _retime_multi_short(candidate: dict, answer: object, words: list[dict], frames: list[dict], game_start: float) -> dict:
    if not isinstance(answer, dict) or answer.get("reject") or not all(
        isinstance(answer.get(key), str) and answer[key].strip() for key in ("title", "story", "hook", "payoff")
    ):
        raise ValueError("Editor must provide the same selected story and its edit")
    proposed = candidate["segments"]
    segments = answer.get("segments")
    if not isinstance(segments, list) or len(segments) != len(proposed):
        raise ValueError("Edit must retain one chronological range per supplied scene")
    first, last = segments[0], segments[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        raise ValueError("Editor returned malformed segments")
    hook = _resolve_evidence(answer.get("hook_evidence") or first.get("hook_evidence"), words, frames,
                             proposed[0]["relative_start"], proposed[0]["relative_end"])
    payoff = _resolve_evidence(answer.get("payoff_evidence") or last.get("payoff_evidence"), words, frames,
                               proposed[-1]["relative_start"], proposed[-1]["relative_end"], prefer_last=True)
    if not hook or not payoff:
        raise ValueError("Hook or payoff evidence is missing from inspected scene context")
    trimmed = []
    for index, (segment, bounds) in enumerate(zip(segments, proposed)):
        if not isinstance(segment, dict):
            raise ValueError("Editor returned a malformed segment")
        # Accept echoed game-relative fields, never absolute source timestamps.
        left, right = segment.get("start", segment.get("relative_start")), segment.get("end", segment.get("relative_end"))
        if type(left) not in (int, float) or type(right) not in (int, float):
            raise ValueError("Editor must use finite game-relative timestamps")
        start, end = float(left), float(right)
        if not math.isfinite(start) or not math.isfinite(end) or start < bounds["relative_start"] - 0.01 \
                or end > bounds["relative_end"] + 0.01 or end - start < 1:
            raise ValueError("Proposed segment lies outside its inspected context or is shorter than one second")
        required_end = end
        if index == 0:
            start = hook[0]["before_time"] if hook[0]["kind"] == "visual" else max(bounds["relative_start"], hook[0]["time"] - 0.25)
            required_end = hook[1]
        if index == len(segments) - 1:
            required_end = payoff[1]
        start, end = _finish_short_range(start, end, words, (bounds["relative_start"], bounds["relative_end"]), required_end)
        if trimmed and start < trimmed[-1]["relative_end"]:
            raise ValueError("Constructed scene ranges overlap")
        trimmed.append({"relative_start": start, "relative_end": end, "source_start": game_start + start, "source_end": game_start + end})
    total = sum(segment["relative_end"] - segment["relative_start"] for segment in trimmed)
    if not 8 <= total <= SHORT_MAX_SECONDS:
        raise ValueError(f"Total retained footage must be 8-{SHORT_MAX_SECONDS} seconds; edit the same story more tightly")
    if not hook or not payoff or not _short_evidence(hook[0], trimmed[0], words, frames, hook=True) \
            or not _short_evidence(payoff[0], trimmed[-1], words, frames, hook=False) or hook[0] == payoff[0]:
        raise ValueError("Constructed scenes do not contain distinct complete hook and payoff evidence")
    return {
        **candidate, "segments": trimmed, "source_start": trimmed[0]["source_start"], "source_end": trimmed[-1]["source_end"],
        "relative_start": trimmed[0]["relative_start"], "relative_end": trimmed[-1]["relative_end"], "duration": total,
        "title": answer["title"][:100], "reason": answer["story"][:240], "hook": answer["hook"][:240],
        "payoff": answer["payoff"][:240], "hook_evidence": hook[0], "payoff_evidence": payoff[0],
    }


def _short_overlap(left: dict, right: dict) -> float:
    return sum(max(0, min(a["relative_end"], b["relative_end"]) - max(a["relative_start"], b["relative_start"]))
               for a in left["segments"] for b in right["segments"]) / min(left["duration"], right["duration"])


def _excluded(start: float, end: float, exclusions: list[dict]) -> bool:
    return any(start < item["end"] and end > item["start"] for item in exclusions)


def _meaningful_streamer_speech(evidence: object, words: list[dict], ranges: list[tuple[float, float]],
                               unhealthy: list[tuple[float, float]]) -> tuple[dict, float] | None:
    if isinstance(evidence, list):
        return next((result for item in evidence if (result := _meaningful_streamer_speech(item, words, ranges, unhealthy))), None)
    if not isinstance(evidence, dict) or evidence.get("kind") != "speech" or len(set(
        re.findall(r"\w+", str(evidence.get("quote", "")).casefold())
    )) < 4:
        return None
    if re.search(r"\b(?:equipo .+ ha ganado|a la batalla|pelea hasta la muerte|tu objetivo ha sido destruido)\b",
                 str(evidence.get("quote", "")).casefold()):
        return None
    for start, end in ranges:
        resolved = _resolve_evidence(evidence, words, [], start, end)
        if resolved and resolved[1] <= end and not any(
            left < resolved[1] and right > resolved[0]["time"] for left, right in unhealthy
        ):
            return resolved
    return None


def _extend_short_payoff(candidate: dict, words: list[dict], game_start: float, limit: float) -> dict:
    """Leave three seconds for the reaction, finishing any sentence at the exit."""
    parts = [dict(part) for part in candidate.get("segments") or [candidate]]
    last = parts[-1]
    target = min(max(last["relative_end"], candidate.get("payoff_end", last["relative_end"]) + 3), limit)
    _, end = _finish_short_range(last["relative_start"], target, words,
                                 (last["relative_start"], limit), target)
    last.update(relative_end=end, source_end=game_start + end)
    if "end" in last:
        last["end"] = end
        last["end_reason"] += "; retained post-event reaction breathing room and complete dialogue"
    return {**candidate, "segments": parts, "relative_end": end, "source_end": game_start + end,
            "duration": sum(part["relative_end"] - part["relative_start"] for part in parts)}


def _edit_selected_short(vod_dir: Path, ai: AI, candidate: dict, game: dict, source: Path,
                         source_tag: str, words: list[dict], cuts: dict, cache_key: dict,
                         exclusions: list[dict], previous: list[dict]) -> dict:
    """Finish the selected story, repairing a cut with expanded context once."""
    if callable(getattr(ai, "refine_long_scene", None)):
        beats = read_json(vod_dir / "beats.json")["beats"]
        scenes, premise_parts, completed_parts = [], [], []
        words = _short_source_words(vod_dir)
        streamer = _meaningful_streamer_speech(candidate.get("streamer_evidence"), words,
                                               [(0, cache_key["editorial_end"] - game["gameplay_start"])], [])
        story_id = _fingerprint({key: value for key, value in candidate.items() if key != "_images"})
        long_path = vod_dir / "long-plan.json"
        long_plan = read_json(long_path) if long_path.exists() else {}
        known_parts = long_plan.get("story_segments", long_plan.get("segments", [])) if _cache_matches(
            long_plan, game_id=game["id"], gameplay_start=game["gameplay_start"], source_identity=cache_key.get("source_identity"),
            beats=cache_key.get("beats"), transcript_edits=cache_key.get("transcript_edits")) else []
        for index, segment in enumerate(candidate["segments"]):
            if len(candidate["segments"]) > 1 and index == 0:
                # The judge's complete spoken premise is already timed. Preserve it;
                # inspect the action separately instead of expanding into all shopping.
                spoken = [word for word in words if word["start"] < segment["relative_end"] and word["end"] > segment["relative_start"]]
                required_end = max((word["end"] for word in spoken), default=segment["relative_end"])
                region = (max(0, segment["relative_start"] - 3), min(cache_key["editorial_end"] - game["gameplay_start"], segment["relative_end"] + 3))
                start, end = _finish_short_range(segment["relative_start"], segment["relative_end"], words, region, required_end)
                premise_parts.append({**segment, "start": start, "end": end, "relative_start": start, "relative_end": end,
                                      "source_start": game["gameplay_start"] + start, "source_end": game["gameplay_start"] + end,
                                      "event_kind": "speech", "start_reason": "Complete selected choice explanation",
                                      "end_reason": "Explanation finishes before the action scene"})
                continue
            known_combat = [part for part in known_parts if part.get("event_kind") == "combat"
                            and part.get("evidence", {}).get("outcome_time", {}).get("kind") == "visual"
                            and part["outcome_time"] - part["action_start"] < SHORT_MAX_SECONDS - sum(p["end"] - p["start"] for p in premise_parts)
                            and part["relative_start"] <= candidate["payoff_evidence"]["time"] < part["relative_end"]
                            and part["relative_start"] < segment["relative_end"] and part["relative_end"] > segment["relative_start"]]
            if known_combat:
                # Reuse a complete, input-bound long-video event instead of asking
                # another model to rediscover its engagement and resolved outcome.
                parent = min(known_combat, key=lambda part: part["end"] - part["start"])
                part = dict(parent)
                if part["start"] <= segment["relative_start"] < part["action_start"]:
                    part["start"] = segment["relative_start"]
                observed = [{"timestamp": item["time"]} for item in part["evidence"].values()
                            if isinstance(item, dict) and item.get("kind") == "visual"]
                decision = _short_frame_decision({"segments": [part]}, observed, words)
                part = decision["segments"][0]
                part["shared_event_source"] = "long-plan.json"
                completed_parts.append(part)
                continue
            included = [beat for beat in beats if beat["start"] < segment["relative_end"] and beat["end"] > segment["relative_start"]]
            scenes.append({"start_beat_id": included[0]["id"], "end_beat_id": included[-1]["id"],
                           "start": segment["relative_start"], "end": segment["relative_end"],
                           "purpose": ("Retain this complete concise explanation: " + candidate["hook"]
                                       if len(candidate["segments"]) > 1 and index == 0 else candidate["reason"]),
                           "required": True, "cache_id": f"{story_id}:{index}",
                           "short_budget": SHORT_MAX_SECONDS - sum(part["end"] - part["start"] for part in premise_parts),
                            "selected_moment_time": candidate["hook_evidence"]["time"] if len(candidate["segments"]) == 1 else candidate["payoff_evidence"]["time"],
                             "required_moment_times": ([candidate["hook_evidence"]["time"], candidate["payoff_evidence"]["time"]]
                                                       if len(candidate["segments"]) == 1 else [candidate["payoff_evidence"]["time"]] if index == len(candidate["segments"]) - 1 else [])
                                                       + ([streamer[0]["time"], streamer[1] - 0.001] if streamer and segment["relative_start"] <= streamer[0]["time"] < segment["relative_end"] else []),
                           "scene_role": "complete_event" if len(candidate["segments"]) == 1 else "premise" if index == 0 else "outcome",
                           "expected_event_kind": "speech" if len(candidate["segments"]) > 1 and index == 0 else "combat" if any(beat.get("type") == "COMBAT" for beat in included) else "speech",
                           "ending_scene": index == len(candidate["segments"]) - 1 and segment["relative_end"] >= game.get("result_end", game["gameplay_end"]) - game["gameplay_start"] - 60,
                            "known_events": [part for part in known_parts if part["relative_start"] < segment["relative_end"] and part["relative_end"] > segment["relative_start"]],
                            "verified_frames": [{"timestamp": evidence["time"], "path": image}
                                                for evidence, image in zip((candidate["hook_evidence"], candidate["payoff_evidence"]), candidate.get("_images", []))],
                            "verified_evidence": {key: candidate[key] for key in ("hook_evidence", "payoff_evidence")},
                            "streamer_evidence": candidate.get("streamer_evidence"),
                            "short_story": {key: candidate[key] for key in ("title", "hook", "payoff")}})
        event_key = {**cache_key, "short_event_version": 8, "model": getattr(ai, "editorial_model", "gpt-5-nano")}
        try:
            parts, _ = _refine_long_scenes(vod_dir, ai, game, beats, scenes, event_key, None, False,
                                         require_intro_hint=False, short=True) if scenes else ([], None)
            parts = sorted([*premise_parts, *completed_parts, *parts], key=lambda part: part["start"])
            if len(candidate["segments"]) == 1 and parts and all(part.get("event_kind") == "combat" for part in parts):
                # Death/spectating are phases of the SAME encounter, not downtime.
                # Keep the action continuous through its final resolved outcome.
                first, last = parts[0], parts[-1]
                first = {**first, "end": last["end"], "outcome_time": last["outcome_time"],
                         "reaction_end": last["reaction_end"], "end_reason": last["end_reason"],
                         "evidence": {**first.get("evidence", {}), **{key: last.get("evidence", {})[key]
                                       for key in ("outcome_time", "reaction_end") if key in last.get("evidence", {})}}}
                if first["action_start"] - first["start"] < 2:
                    opening_beat = next(beat for beat in beats if beat["end"] > first["start"])
                    region = (max(0, opening_beat["start"] - 3), first["end"] + 0.1)
                    first["start"], _ = _avoid_split_word(min(first["start"], opening_beat["start"]), first["end"], words, region)
                    first["start_reason"] = "Retain the selected beat's pre-engagement approach before the observed action"
                parts = [first]
            observed = [{"timestamp": part[edge]} for part in parts for edge in ("start", "end")]
            parts = _fit_short_event_budget(parts, SHORT_MAX_SECONDS, words, observed, candidate["hook_evidence"]["time"]
                                           if len(candidate["segments"]) == 1 else candidate["payoff_evidence"]["time"])
            parts = [{**part, "relative_start": part["start"], "relative_end": part["end"],
                      "source_start": game["gameplay_start"] + part["start"], "source_end": game["gameplay_start"] + part["end"]} for part in parts]
            total = sum(part["source_end"] - part["source_start"] for part in parts)
            _check_short_moments(parts, [candidate["hook_evidence"]["time"], candidate["payoff_evidence"]["time"]])
            if candidate.get("streamer_evidence") and not _meaningful_streamer_speech(
                candidate["streamer_evidence"], words, [(part["start"], part["end"]) for part in parts], []
            ):
                raise ValueError("Retain the complete meaningful streamer speech selected for this short")
            cut = {**candidate, "segments": parts, "duration": total,
                   **{key: parts[0][key] for key in ("source_start", "relative_start")},
                   **{key: parts[-1][key] for key in ("source_end", "relative_end")}}
            source_duration = read_json(vod_dir / "manifest.json")["duration"]
            edit_limit = min(source_duration, cache_key["editorial_end"] if "result_end" in game else
                             min(game.get("postgame_end", cache_key["editorial_end"] + 15), cache_key["editorial_end"] + 15))
            _validate_short_candidates([cut], game["gameplay_start"], edit_limit, source_duration)
            if any(_excluded(part["relative_start"], part["relative_end"], exclusions) for part in parts):
                raise ValueError("Complete event enters excluded footage")
            if any(_short_overlap(cut, item) >= 0.5 for item in previous):
                raise ValueError("Complete event overlaps the other selected short")
            return cut
        except ValueError as error:
            write_json(vod_dir / "short-editing-status.json", {"game_id": game["id"], "title": candidate["title"],
                       "state": "incomplete", "problem": str(error)})
            raise
    game_start = game["gameplay_start"]
    game_duration = game.get("result_end", game["gameplay_end"]) - game_start
    feedback = None
    for attempt in range(2):
        frames, context_segments = [], []
        single = len(candidate["segments"]) == 1
        lead, tail = ((20 + attempt * 15, 45 + attempt * 15) if single else (3 + attempt * 12, 3 + attempt * 12))
        for index, segment in enumerate(candidate["segments"]):
            left = ((candidate["segments"][index - 1]["relative_end"] + segment["relative_start"]) / 2 if index else 0)
            right = ((segment["relative_end"] + candidate["segments"][index + 1]["relative_start"]) / 2
                     if index + 1 < len(candidate["segments"]) else game_duration)
            left = max([left, *(item["end"] for item in exclusions if item["end"] <= segment["relative_start"])])
            right = min([right, *(item["start"] for item in exclusions if item["start"] >= segment["relative_end"])])
            start, end = max(left, segment["relative_start"] - lead), min(right, segment["relative_end"] + tail)
            context_segments.append({"relative_start": start, "relative_end": end,
                                     "source_start": game_start + start, "source_end": game_start + end})
            label = "cuts" if single else "refine"
            frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{source_tag}-{label}-{start:.2f}-{end:.2f}"
            sampled = sorted(frame_dir.glob("frame_*.jpg"))
            if not sampled:
                sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=game_start + start, end=game_start + end)]
            positions = {round(step * (len(sampled) - 1) / 21) for step in range(22)} if sampled else set()
            for evidence in (candidate["hook_evidence"], candidate["payoff_evidence"]):
                if start <= evidence["time"] < end:
                    center = round(evidence["time"] - start)
                    positions.update(range(max(0, center - 2), min(len(sampled), center + 3)))
            frames.extend({"timestamp": start + position, "path": str(sampled[position].resolve())} for position in sorted(positions))
        relevant_words = [word for word in words if any(float(word["end"]) > segment["relative_start"]
                          and float(word["start"]) < segment["relative_end"] for segment in context_segments)]
        context = {**candidate, "segments": context_segments, "rough_segments": candidate["segments"],
                   "editing_feedback": feedback,
                   "avoid_segments": [part for item in previous for part in item["segments"]]}
        cut_key = _fingerprint({key: value for key, value in context.items() if key != "_images"})
        if cut_key not in cuts:
            cuts[cut_key] = ai.refine_short(context, relevant_words, frames)
            write_json(vod_dir / "short-cuts.json", {**cache_key, "cuts_version": 10, "answers": cuts})
        try:
            if single:
                region = (context_segments[0]["relative_start"], context_segments[0]["relative_end"])
                cut = _retime_short(context, cuts[cut_key], relevant_words, frames, game_start, region)
            else:
                cut = _retime_multi_short(context, cuts[cut_key], relevant_words, frames, game_start)
            if any(_excluded(part["relative_start"], part["relative_end"], exclusions) for part in cut["segments"]):
                raise ValueError("Edit enters excluded footage; keep the same story outside excluded ranges")
            if any(_short_overlap(cut, item) >= 0.5 for item in previous):
                raise ValueError("Edit repeats the other selected moment; remove duplicated setup using avoid_segments")
            cut["_images"] = [min(frames, key=lambda frame: abs(frame["timestamp"] - evidence["time"]))["path"]
                              for evidence in (cut["hook_evidence"], cut["payoff_evidence"])]
            for key in ("rough_segments", "editing_feedback", "avoid_segments"):
                cut.pop(key, None)
            return cut
        except ValueError as error:
            answer = cuts[cut_key]
            feedback = str(error) + (f". Editor reported: {answer.get('reason')}" if isinstance(answer, dict) and answer.get("reason") else "")
            write_json(vod_dir / "short-editing-status.json", {"game_id": game["id"], "title": candidate["title"],
                       "state": "repairing" if attempt == 0 else "incomplete", "attempt": attempt + 1, "problem": feedback})
    raise ValueError(f"Selected short '{candidate['title']}' still needs editing: {feedback}. "
                     "Selection was preserved; see short-editing-status.json and cached short-cuts.json.")


def _unhealthy_transcript_ranges(vod_dir: Path, beats: list[dict], game_duration: float) -> list[tuple[float, float]]:
    review = vod_dir / "transcript.reviewed.json"
    ranges = [
        (max(0.0, float(item["start"])), min(game_duration, float(item["end"])))
        for item in (read_json(review).get("exclude_ranges", []) if review.exists() else [])
        if isinstance(item, dict) and type(item.get("start")) in (int, float) and type(item.get("end")) in (int, float)
    ]
    run: list[dict] = []
    for beat in [*beats, {"start": game_duration, "end": game_duration, "text": "healthy"}]:
        tokens = str(beat.get("text", "")).casefold().split()
        unhealthy = not tokens or (len(tokens) >= 4 and len(set(tokens)) <= max(1, len(tokens) // 5))
        if unhealthy:
            run.append(beat)
        else:
            if run and run[-1]["end"] - run[0]["start"] >= 30:
                ranges.append((max(0, run[0]["start"] - 10), min(game_duration, run[-1]["end"] + 10)))
            run = []
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def find_shorts(vod_dir: Path, ai: AI, force: bool = False) -> list[dict]:
    output = vod_dir / "short-candidates.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    source = Path(read_json(vod_dir / "manifest.json")["source"])
    source_identity = _source_identity(source)
    source_tag = _fingerprint(source_identity)[:8]
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
    cache_key = {"version": 8, "game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end,
                 "exclusions": exclusions, "transcript_edits": _transcript_edits(vod_dir),
                 "beats": _file_fingerprint(vod_dir / "beats.json"),
                 "source_identity": source_identity}
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key, evidence_version=7, cuts_version=10, output_version=21, callback_version=2):
            return cached["candidates"]
    beats = [beat for beat in _selected_beats(vod_dir, {
        "game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end,
        "transcript_edits": cache_key["transcript_edits"],
        "source_identity": source_identity,
    }) if beat["start"] >= 0]
    by_id = {beat["id"]: index for index, beat in enumerate(beats)}
    proposals_path = vod_dir / "short-proposals.json"
    cached_proposals = read_json(proposals_path) if proposals_path.exists() else {}
    if not force and _cache_matches(cached_proposals, **cache_key, proposal_version=3):
        proposals = cached_proposals["candidates"]
    else:
        if force:
            (vod_dir / "short-proposal-windows.json").unlink(missing_ok=True)
        proposals = ai.shorts([beat for beat in beats if not _excluded(beat["start"], beat["end"], exclusions)
                              and (game.get("league_ranges") is None or any(
                                  item["start"] <= game["gameplay_start"] + beat["start"] < item["end"]
                                  for item in game["league_ranges"]))])
        write_json(proposals_path, {**cache_key, "proposal_version": 3, "candidates": proposals})
    frame_dir = vod_dir / "frames" / "selected-game"
    frames = sorted(frame_dir.glob("frame_*.jpg"))
    champ_select = game.get("champ_select_start")
    pregame_start = max(float(champ_select), game["gameplay_start"] - 180) if champ_select is not None and champ_select < game["gameplay_start"] else game["gameplay_start"]
    interval = read_json(vod_dir / "beats.json").get("target_beat_seconds", 15) / 3
    frame_index = frame_dir / "index.json"
    stamped = read_json(frame_index)["frames"] if frame_index.exists() else [{"timestamp": pregame_start + index * interval - game["gameplay_start"], "path": str(path)} for index, path in enumerate(frames)]
    game_duration = editorial_end - game["gameplay_start"]
    unhealthy_runs = [item for item in _unhealthy_transcript_ranges(vod_dir, beats, game_duration) if not _excluded(*item, exclusions)]
    # Discovery is text-led; inspect images only for this bounded shortlist.
    proposals = list({json.dumps(item, sort_keys=True, ensure_ascii=False): item
                     for item in proposals if isinstance(item, dict)}.values())[:6]
    candidates = []
    rejected: list[dict] = []
    words = transcript_words(vod_dir)
    verdicts_path = vod_dir / "short-verdicts.json"
    cached_verdicts = read_json(verdicts_path) if verdicts_path.exists() else {}
    verdicts = cached_verdicts.get("verdicts", {}) if _cache_matches(cached_verdicts, **cache_key, evidence_version=7) and not force else {}
    timing_path = vod_dir / "short-visual-timing.json"
    cached_timing = read_json(timing_path) if timing_path.exists() else {}
    timing = cached_timing.get("answers", {}) if not force and _cache_matches(cached_timing, **cache_key, timing_version=1) else {}
    prepared = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        scenes = proposal.get("scenes", [])
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 4:
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
        if not 1 <= len(ranges) <= 4 or sum(end - start for start, end in ranges) > 120 or type(payoff_id) is not int or payoff_id not in by_id or any(_excluded(start, end, exclusions) for start, end in ranges):
            continue
        if game.get("league_ranges") is not None and any(not any(
            item["start"] <= game["gameplay_start"] + start < game["gameplay_start"] + end <= item["end"]
            for item in game["league_ranges"]
        ) for start, end in ranges):
            rejected.append({"title": str(proposal.get("title", "Untitled")), "stage": "verify",
                             "reason": "Outside League of Legends game/draft footage"})
            continue
        payoff_beat = beats[by_id[payoff_id]]
        if not (ranges[-1][0] <= payoff_beat["start"] and payoff_beat["end"] <= ranges[-1][1]):
            continue
        verdict_key = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
        frames = []
        for index, (start, end) in enumerate(ranges):
            frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{source_tag}-{start:.2f}-{end:.2f}-1s"
            sampled = sorted(frame_dir.glob("frame_*.jpg"))
            if not sampled:
                sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=game["gameplay_start"] + start, end=game["gameplay_start"] + end)]
            if not sampled:
                break
            if index == len(ranges) - 1:
                first = max(0, int(payoff_beat["start"] - start) - 5)
                last = min(len(sampled) - 1, int(payoff_beat["end"] - start) + 2)
                positions = {round(first + step * (last - first) / 11) for step in range(12)}
                positions.update(range(max(0, len(sampled) - 3), len(sampled)))
            else:
                positions = {0, len(sampled) // 2, len(sampled) - 1}
            if index == 0:
                positions.update(range(min(3, len(sampled))))
            frames.extend({"timestamp": round(start + position, 2), "path": str(sampled[position])} for position in sorted(positions))
        frames.extend(frame for frame in stamped if ranges[-1][0] <= frame["timestamp"] < ranges[-1][1])
        frames.sort(key=lambda frame: frame["timestamp"])
        if not frames:
            continue
        anchors = [edge for start, end in ranges for edge in (start, end)] + [payoff_beat["start"], payoff_beat["end"]]
        prepared.append((proposal, ranges, payoff_beat, _bounded_frames(frames, anchors), verdict_key))
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {}
        for proposal, ranges, _, frames, verdict_key in prepared:
            if verdict_key not in verdicts:
                context = [beat for beat in beats if any(beat["start"] < end and beat["end"] > start for start, end in ranges)]
                future = pool.submit(ai.verify_short, {**proposal, "scenes": [{"start": start, "end": end} for start, end in ranges]}, context, frames)
                pending[future] = verdict_key
        for future in as_completed(pending):
            verdicts[pending[future]] = future.result()
            write_json(verdicts_path, {**cache_key, "evidence_version": 7, "verdicts": verdicts})
    for proposal, ranges, payoff_beat, frames, verdict_key in prepared:
        verdict = verdicts[verdict_key]
        if not isinstance(verdict, dict) or verdict.get("reject"):
            rejected.append({"title": str(proposal.get("title", "Untitled")), "stage": "verify",
                             "reason": str(verdict.get("reason", "Evidence gate rejected")) if isinstance(verdict, dict) else "Malformed verdict"})
            continue
        if not all(isinstance(verdict.get(key), str) and verdict[key].strip() for key in ("hook", "payoff", "story")):
            continue
        segments = verdict.get("segments")
        if not isinstance(segments, list):
            continue
        if len(ranges) == 1 and 1 < len(segments) <= 4 and all(
            isinstance(part, dict) and type(part.get("start")) in (int, float) and type(part.get("end")) in (int, float)
            and math.isfinite(part["start"]) and math.isfinite(part["end"])
            and ranges[0][0] <= part["start"] < part["end"] <= ranges[0][1]
            for part in segments
        ) and all(abs(left["end"] - right["start"]) < 0.01 for left, right in zip(segments, segments[1:])):
            # Adjacent model segments are one continuous story inside the search window.
            segments = [{"start": segments[0]["start"], "end": segments[-1]["end"]}]
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
        if len(trimmed) != len(ranges) or not 8 <= total <= SHORT_MAX_SECONDS:
            continue
        hook = _resolve_evidence(verdict.get("hook_evidence"), words, frames, ranges[0][0], ranges[0][1])
        payoff_options = verdict.get("payoff_evidence")
        if proposal.get("origin") == "callback" and "ACE" in str(proposal.get("proposed_payoff", "")).upper() and isinstance(payoff_options, list):
            payoff_options = [item for item in payoff_options if isinstance(item, dict) and "ACE" in str(item.get("detail", "")).upper()]
        payoff = _resolve_evidence(payoff_options, words, frames, ranges[-1][0], ranges[-1][1], prefer_last=True)
        if not hook or not payoff:
            continue
        hook_evidence, payoff_evidence = hook[0], payoff[0]
        streamer = _meaningful_streamer_speech(verdict.get("streamer_evidence"), words, ranges, unhealthy_runs)
        if not getattr(ai, "mock", True) and not streamer:
            rejected.append({"title": str(proposal.get("title", "Untitled")), "stage": "verify",
                             "reason": "No meaningful verified Elmiillor speech"})
            continue
        if any(evidence["kind"] == "speech" and any(start <= evidence["time"] < end for start, end in unhealthy_runs)
               for evidence in (hook_evidence, payoff_evidence)):
            rejected.append({"title": str(proposal.get("title", "Untitled")), "stage": "verify",
                             "reason": "Spoken premise/payoff relies on an unhealthy repetitive transcript"})
            continue
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
        # The verifier chose a complete setup. An anchor is not permission to
        # discard everything before that word/frame or chop off its full payoff.
        trimmed[0]["relative_start"] = min(trimmed[0]["relative_start"], hook_evidence.get("before_time", hook_evidence["time"]))
        trimmed[0]["source_start"] = game["gameplay_start"] + trimmed[0]["relative_start"]
        trimmed[-1]["relative_end"] = max(trimmed[-1]["relative_end"], payoff[1])
        trimmed[-1]["source_end"] = game["gameplay_start"] + trimmed[-1]["relative_end"]
        if streamer:
            for part, (start, end) in zip(trimmed, ranges):
                if start <= streamer[0]["time"] < end:
                    part["relative_start"] = min(part["relative_start"], streamer[0]["time"])
                    part["relative_end"] = max(part["relative_end"], streamer[1])
                    part["source_start"] = game["gameplay_start"] + part["relative_start"]
                    part["source_end"] = game["gameplay_start"] + part["relative_end"]
        total = sum(segment["relative_end"] - segment["relative_start"] for segment in trimmed)
        if not 8 <= total <= SHORT_MAX_SECONDS or any(part["relative_end"] - part["relative_start"] < 1 for part in trimmed):
            continue
        if not _short_evidence(hook_evidence, trimmed[0], words, frames, hook=True) or not _short_evidence(payoff_evidence, trimmed[-1], words, frames, hook=False):
            continue
        if hook_evidence == payoff_evidence:
            continue
        callback = proposal.get("origin") == "callback"
        candidates.append(
            {
                "segments": trimmed, "source_start": trimmed[0]["source_start"], "source_end": trimmed[-1]["source_end"],
                "relative_start": trimmed[0]["relative_start"], "relative_end": trimmed[-1]["relative_end"],
                "duration": total, "title": (f"{verdict['hook']} → ACE" if callback and "ACE" in str(proposal.get("proposed_payoff", "")) else str(verdict.get("title", proposal.get("title", "Untitled"))))[:100],
                "reason": str(f"{verdict['hook']} → {payoff_evidence['detail']}" if callback and payoff_evidence["kind"] == "visual" else verdict["story"])[:240], "hook": str(verdict["hook"])[:240],
                "payoff": str(payoff_evidence["detail"] if callback and payoff_evidence["kind"] == "visual" else verdict["payoff"])[:240], "hook_evidence": hook_evidence,
                "payoff_evidence": payoff_evidence,
                "payoff_end": payoff[1],
                **({"streamer_evidence": streamer[0]} if streamer else {}),
                "_images": [min(frames, key=lambda frame: abs(frame["timestamp"] - moment["time"]))["path"] for moment in (hook_evidence, payoff_evidence)],
            }
        )
    # Judge complete source moments before spending calls on shorts-specific cuts.
    # ponytail: pairwise alias collapse; interval grouping if candidate pools become large.
    unique = []
    for candidate in sorted(candidates, key=lambda item: item["duration"], reverse=True):
        if not any(_short_overlap(candidate, previous) >= 0.5 for previous in unique):
            unique.append(candidate)
    candidates = sorted(unique, key=lambda item: item["source_start"])
    write_json(vod_dir / "short-verified-moments.json", {**cache_key, "candidates": candidates})
    choices_path = vod_dir / "short-choices.json"
    cached_choices = read_json(choices_path) if choices_path.exists() else {}
    candidate_signature = _fingerprint([{key: value for key, value in item.items() if key != "_images"} for item in candidates])
    if not force and _cache_matches(cached_choices, **cache_key, candidate_signature=candidate_signature, judge_version=4):
        chosen = cached_choices["selected_ids"]
    else:
        evidence = [{"candidate_id": index, **{key: value for key, value in item.items() if key != "_images"}} for index, item in enumerate(candidates)]
        chosen = ai.choose_shorts(evidence, [Path(image) for item in candidates for image in item["_images"]]) if candidates else []
        write_json(choices_path, {**cache_key, "candidate_signature": candidate_signature, "judge_version": 4, "selected_ids": chosen})
    ranked_ids = []
    for index in chosen if isinstance(chosen, list) else []:
        if type(index) is int and 0 <= index < len(candidates) and index not in ranked_ids:
            ranked_ids.append(index)
    # A weak stream still gets its best available factual story, not a score gate.
    ranked_ids.extend(index for index in range(len(candidates)) if index not in ranked_ids)
    ranked = [candidates[index] for index in ranked_ids]
    rejected.extend({"title": item["title"], "stage": "rank", "reason": "Not recommended by the source-moment judge"}
                    for index, item in enumerate(candidates) if index not in ranked_ids)
    gated = ranked
    # Factual review already supplied complete cuts. Finish words/reactions in
    # code instead of buying another discovery + dense re-edit of the same story.

    source_duration = read_json(vod_dir / "manifest.json").get("duration", editorial_end)
    edit_limit = min(source_duration, editorial_end if "result_end" in game else
                     min(game.get("postgame_end", editorial_end + 15), editorial_end + 15))
    extended = []
    for candidate in gated:
        limit = edit_limit - game["gameplay_start"]
        if game.get("league_ranges") is not None:
            limit = min(limit, next(item["end"] - game["gameplay_start"] for item in game["league_ranges"]
                                    if item["start"] <= candidate["source_end"] <= item["end"]))
        limit = min([limit, *(item["start"] for item in exclusions if item["start"] >= candidate["relative_end"])])
        previous_duration = sum(part["relative_end"] - part["relative_start"] for part in candidate["segments"][:-1])
        limit = min(limit, candidate["segments"][-1]["relative_start"] + SHORT_MAX_SECONDS - previous_duration)
        finished = _extend_short_payoff(candidate, words, game["gameplay_start"], limit)
        if finished["relative_end"] < candidate["payoff_end"] or finished["duration"] > SHORT_MAX_SECONDS:
            rejected.append({"title": candidate["title"], "stage": "edit", "reason": "Complete payoff does not fit 60 seconds"})
            continue
        extended.append(finished)
    gated = extended
    _validate_short_candidates(gated[:2], game["gameplay_start"], edit_limit, source_duration)

    selected_ids = []
    for index in range(len(gated)):
        candidate = gated[index]
        if any(
            _short_overlap(candidate, gated[previous]) >= 0.5
            for previous in selected_ids
        ):
            continue
        selected_ids.append(index)
        if len(selected_ids) == 2:
            break
    selected = [
        {**{key: value for key, value in gated[index].items() if key != "_images"},
         "evidence_images": gated[index].get("_images", []),
         "output_number": output_number, "render_name": f"short-{output_number:02d}"}
        for output_number, index in enumerate(selected_ids, 1)
    ]
    selected_set = set(selected_ids)
    rejected.extend({"title": item["title"], "stage": "rank", "reason": "Passed quality gates but was not selected"}
                    for index, item in enumerate(gated) if index not in selected_set)
    validation_rejected = max(0, len(proposals) - len(candidates) - sum(item["stage"] == "verify" for item in rejected))
    if validation_rejected:
        rejected.append({"title": "Structural/evidence validation", "stage": "verify",
                         "reason": f"{validation_rejected} proposal(s) failed scene, duration, timestamp, or evidence checks"})
    write_json(vod_dir / "short-diagnostics.json", {**cache_key, "proposal_count": len(proposals), "verified_count": len(candidates),
                                                      "validation_rejected_count": validation_rejected,
                                                      "ranked_count": len(ranked), "edited_count": len(gated),
                                                      "gated_count": len(gated), "selected_count": len(selected), "rejected": rejected})
    write_json(output, {**cache_key, "evidence_version": 7, "cuts_version": 10, "output_version": 21, "callback_version": 2,
                       "edit_end": max([editorial_end, *(candidate["source_end"] for candidate in selected)]), "candidates": selected})
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
        str(word["text"]).translate(str.maketrans("", "", "!¡.")).upper().replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
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


def _short_source_words(vod_dir: Path) -> list[dict]:
    """Share cached game/result-tail speech between event editing and captions."""
    candidates_path = vod_dir / "short-candidates.json"
    raw = read_json(candidates_path) if candidates_path.exists() else {}
    if raw.get("scope") == "vod":
        return _short_source_words(Path(raw["workspace"]))
    transcript = transcript_words(vod_dir)
    tail_path = vod_dir / "long-ending-transcript.json"
    if tail_path.exists():
        tail = read_json(tail_path)
        game = selected_game(vod_dir)
        if tail.get("audio_start") == game["gameplay_end"] and tail.get("source_identity") == _source_identity(Path(read_json(vod_dir / "manifest.json")["source"])):
            offset = tail["audio_start"] - game["gameplay_start"]
            transcript.extend({**word, "start": word["start"] + offset, "end": word["end"] + offset}
                              for word in tail.get("words", []) if word["start"] + offset >= game["gameplay_end"] - game["gameplay_start"])
    return sorted(transcript, key=lambda word: float(word["start"]))


def _short_caption_words(vod_dir: Path, candidate: dict, ai: AI | None = None) -> list[dict]:
    """Cache punctuation-only corrections without altering speech, language or timing."""
    segments = candidate.get("segments") or [candidate]
    transcript = _short_source_words(vod_dir)
    words = [{**word} for segment in segments for word in transcript
             if float(word["start"]) < segment["relative_end"] and float(word["end"]) > segment["relative_start"]]
    signature = _fingerprint({"segments": [{key: segment[key] for key in ("relative_start", "relative_end")} for segment in segments], "words": words})
    name = candidate.get("render_name", "short")
    path = vod_dir / f"{name}.captions.raw.json"
    cached = read_json(path) if path.exists() else {}
    model = getattr(ai, "model", "gpt-5-nano") if ai else None
    matches = _cache_matches(cached, version=3, input_signature=signature) and (model is None or cached.get("model") == model)
    if not matches:
        if ai is None or not callable(getattr(ai, "punctuate_captions", None)):
            return words
        answer = ai.punctuate_captions(words) if words else {"text": ""}
        cached = {"version": 3, "input_signature": signature, "model": model, "answer": answer}
        write_json(path, cached)
    answer = cached.get("answer") if isinstance(cached.get("answer"), dict) else {}
    tokens = answer["text"].split() if isinstance(answer.get("text"), str) else answer.get("words", [])
    tokens = [text for text in tokens if isinstance(text, str) and text.strip()] if isinstance(tokens, list) else []
    normalize = lambda text: re.sub(r"\W", "", str(text).casefold())
    originals = [str(word["word"]).strip() for word in words]
    # Project punctuation onto matching SOURCE words. Model insertions, deletions
    # or translations never enter the captions or invalidate an already edited clip.
    matcher = SequenceMatcher(None, [normalize(text) for text in originals], [normalize(text) for text in tokens], autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            words[block.a + offset]["word"] = tokens[block.b + offset].strip()
    pending = []
    for index, (word, original) in enumerate(zip(words, originals)):
        if any(mark in original for mark in "¡¿") and pending and index:
            for mark in pending:
                if mark not in words[index - 1]["word"]:
                    words[index - 1]["word"] = str(words[index - 1]["word"]).rstrip(".,:;") + mark
            pending.clear()
        pending.extend("!" if mark == "¡" else "?" for mark in original if mark in "¡¿")
        word["word"] = str(word["word"]).replace("¡", "").replace("¿", "")
        pending = [mark for mark in pending if mark not in word["word"]]
        ends_phrase = index == len(words) - 1 or float(words[index + 1]["start"]) - float(word["end"]) >= 0.65
        if ends_phrase:
            for mark in pending:
                word["word"] = word["word"].rstrip(".,:;") + mark
            pending.clear()
    return words


def _write_short_subtitles(vod_dir: Path, candidate: dict) -> Path:
    segments = candidate.get("segments") or [candidate]
    render_name = candidate.get("render_name")
    specific_review = vod_dir / f"{render_name}.captions.reviewed.json" if render_name else None
    review = specific_review if specific_review and specific_review.exists() else vod_dir / "short-captions.reviewed.json"
    reviewed = read_json(review) if review.exists() else {}
    if reviewed.get("segments") == segments and reviewed.get("segments") is not None:
        subtitle_blocks = reviewed.get("blocks", [])
    elif len(segments) == 1 and not candidate.get("segments") and reviewed.get("source_start") == candidate["source_start"] and reviewed.get("source_end") == candidate["source_end"]:
        subtitle_blocks = reviewed.get("blocks", [])
    else:
        subtitle_blocks = []
        offset = 0.0
        words = _short_caption_words(vod_dir, candidate)
        for segment in segments:
            selected = [word for word in words if float(word.get("start", 0)) < segment["relative_end"] and float(word.get("end", 0)) > segment["relative_start"]]
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
    output = vod_dir / f"{render_name}.ass" if render_name else vod_dir / "short.ass"
    output.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{ASS_NAME},96,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1\n\n"
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


def _short_layout(vod_dir: Path) -> dict:
    reviewed = vod_dir / "layout.reviewed.json"
    layout = read_json(reviewed if reviewed.exists() else vod_dir / "layout.json")
    # Bias gameplay left so the champion-stats panel remains inside the crop.
    return {"gameplay": [0.22, 0, 0.49, 1], **layout}


def render_short(vod_dir: Path, candidate_index: int = 0) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    candidates = short_candidates(vod_dir)
    if not candidates:
        raise SystemExit("No short candidates are available")
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise SystemExit(f"Short candidate index out of range: {candidate_index}")
    candidate = candidates[candidate_index]
    render_name = candidate.get("render_name") or f"short-{candidate.get('output_number', candidate_index + 1):02d}"
    # Resuming a batch re-renders every short unless the sidecar still proves this one.
    already = next((item["render"] for item in validated_short_renders(vod_dir)
                    if item["render_name"] == render_name), None)
    if already is not None:
        return already
    layout = _short_layout(vod_dir)
    subtitles = _write_short_subtitles(vod_dir, {**candidate, "render_name": render_name})
    output = vod_dir / "renders" / f"{render_name}.mp4"
    output.with_suffix(".json").unlink(missing_ok=True)
    render_vertical(Path(manifest["source"]), candidate.get("segments") or [candidate],
                    layout["facecam"], layout["gameplay"], subtitles, output,
                    layout.get("hud"))
    expected_duration = sum(segment["source_end"] - segment["source_start"] for segment in candidate.get("segments") or [candidate])
    info = media_info(output)
    if abs(info["duration"] - expected_duration) > 0.5 or (info["width"], info["height"]) != (1080, 1920) or not info["audio"]:
        raise RuntimeError(f"Invalid short render {output}: expected {expected_duration:.2f}s 1080x1920 with audio, got {info}")
    source = Path(manifest["source"])
    source_stat = source.stat()
    write_json(output.with_suffix(".json"), {
        "render_version": 2,
        "candidate": _fingerprint(candidate),
        "captions": _file_fingerprint(subtitles),
        "caption_text": _file_fingerprint(vod_dir / f"{render_name}.captions.raw.json"),
        "caption_review": _file_fingerprint(vod_dir / f"{render_name}.captions.reviewed.json")
                          or _file_fingerprint(vod_dir / "short-captions.reviewed.json"),
        "layout": _fingerprint(layout),
        "source": {"path": str(source.resolve()), "size": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns},
        "expected_duration": expected_duration,
        "output": _media_fingerprint(output),
        **info,
    })
    return output


def render_shorts(vod_dir: Path) -> list[Path]:
    return [render_short(vod_dir, index) for index in range(len(short_candidates(vod_dir)))]


def validated_short_renders(vod_dir: Path) -> list[dict]:
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    source_stat = source.stat()
    layout = _short_layout(vod_dir)
    source_identity = {"path": str(source.resolve()), "size": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns}
    renders = []
    for index, item in enumerate(short_candidates(vod_dir), 1):
        render_name = item.get("render_name") or f"short-{item.get('output_number', index):02d}"
        sidecar_path = vod_dir / "renders" / f"{render_name}.json"
        render_path = vod_dir / "renders" / f"{render_name}.mp4"
        captions_path = vod_dir / f"{render_name}.ass"
        specific_review = vod_dir / f"{render_name}.captions.reviewed.json"
        caption_review = specific_review if specific_review.exists() else vod_dir / "short-captions.reviewed.json"
        sidecar = read_json(sidecar_path) if sidecar_path.exists() else {}
        expected_duration = sum(segment["source_end"] - segment["source_start"] for segment in item.get("segments") or [item])
        valid = render_path.exists() and sidecar.get("render_version") == 2 \
            and sidecar.get("output") == _media_fingerprint(render_path) and sidecar.get("candidate") == _fingerprint(item) \
            and sidecar.get("captions") == _file_fingerprint(captions_path) and sidecar.get("layout") == _fingerprint(layout) \
            and sidecar.get("caption_review") == _file_fingerprint(caption_review) \
            and sidecar.get("caption_text") == _file_fingerprint(vod_dir / f"{render_name}.captions.raw.json") \
            and sidecar.get("source") == source_identity \
            and abs(float(sidecar.get("expected_duration", -1)) - expected_duration) <= 0.01 \
            and abs(float(sidecar.get("duration", -1)) - expected_duration) <= 0.5 \
            and sidecar.get("width") == 1080 and sidecar.get("height") == 1920 and sidecar.get("audio") is True
        if not valid:
            continue
        renders.append({"candidate": item, "render": render_path, "render_name": render_name,
                        "duration": float(sidecar["duration"]), "width": 1080, "height": 1920})
    return renders
