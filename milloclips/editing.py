from __future__ import annotations

import json
import hashlib
import math
import re
import struct
import tempfile
import unicodedata
import wave
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
from yt_dlp import YoutubeDL

from .ai import AI
from .fonts import ASS_NAME, FILE
from .io import read_json, write_json
from .media import duration, extract_audio, ffmpeg, media_info, render_long_video, render_segments, render_vertical, run_ffmpeg, sample_frames


ACCENT = (234, 255, 32)


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
        if not 8 <= total <= 60:
            raise ValueError("Short duration must be between 8 and 60 seconds")
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
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    exclusions_path = vod_dir / "short-exclusions.reviewed.json"
    exclusions_review = read_json(exclusions_path) if exclusions_path.exists() else {}
    exclusions = exclusions_review.get("ranges", []) if exclusions_review.get("game_id", game["id"]) == game["id"] else None
    manifest = read_json(vod_dir / "manifest.json")
    source_identity = _source_identity(Path(manifest["source"]))
    if not _cache_matches(raw, version=8, output_version=10, evidence_version=3, cuts_version=5, callback_version=2,
                          game_id=game["id"], gameplay_start=game["gameplay_start"], editorial_end=editorial_end,
                          exclusions=exclusions, transcript_edits=_transcript_edits(vod_dir),
                          beats=_file_fingerprint(vod_dir / "beats.json"),
                          source_identity=source_identity):
        raise SystemExit("short-candidates.json is stale; run find-shorts again")
    if reviewed.exists():
        correction = read_json(reviewed)
        if correction.get("version") != raw.get("version") or correction.get("game_id") != raw.get("game_id") \
                or correction.get("raw_signature") != _fingerprint(raw.get("candidates", [])):
            raise SystemExit("short-candidates.reviewed.json is stale or malformed; update its version, game_id, and raw_signature or remove it")
        return _validate_short_candidates(correction.get("candidates"), game["gameplay_start"], editorial_end, manifest["duration"])
    return _validate_short_candidates(raw.get("candidates"), game["gameplay_start"], editorial_end, manifest["duration"])


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


def _long_scene_ranges(beats: list[dict], model_plan: dict, *, target: float = 530, maximum: float = 690) -> list[dict]:
    by_id = {beat["id"]: index for index, beat in enumerate(beats)}
    scenes = []
    proposals = sorted(
        (proposal for proposal in model_plan.get("scenes", []) if isinstance(proposal, dict)
         and proposal.get("start_beat_id") in by_id),
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

    boring = {"FARMING", "TRAVEL", "SHOPPING", "DEAD"}
    for scene in scenes[1:]:
        while scene["first_index"] < scene["last_index"] and beats[scene["first_index"]].get("type") in boring \
                and beats[scene["first_index"]].get("importance", 1) < 3:
            scene["first_index"] += 1
        while scene["first_index"] < scene["last_index"] and beats[scene["last_index"]].get("type") in boring \
                and beats[scene["last_index"]].get("importance", 1) < 3:
            scene["last_index"] -= 1

    for scene in scenes:
        if scene["end"] - scene["start"] < 35:
            scene["first_index"] = max(0, scene["first_index"] - 1)
            scene["last_index"] = min(len(beats) - 1, scene["last_index"] + 1)

    def merge(items: list[dict]) -> list[dict]:
        merged = []
        for scene in sorted(items, key=lambda item: item["first_index"]):
            if merged and scene["first_index"] <= merged[-1]["last_index"] + 1:
                merged[-1]["last_index"] = max(merged[-1]["last_index"], scene["last_index"])
                merged[-1]["purpose"] = f"{merged[-1]['purpose']}; {scene['purpose']}"[:240]
                merged[-1]["required"] = merged[-1].get("required", False) or scene.get("required", False)
            else:
                merged.append(dict(scene))
        for scene in merged:
            scene.update(start_beat_id=beats[scene["first_index"]]["id"], end_beat_id=beats[scene["last_index"]]["id"],
                         start=float(beats[scene["first_index"]]["start"]), end=float(beats[scene["last_index"]]["end"]))
        return merged

    scenes = merge(scenes)
    while sum(scene["end"] - scene["start"] for scene in scenes) < target:
        covered = {index for scene in scenes for index in range(scene["first_index"], scene["last_index"] + 1)}
        candidates = sorted(
            (index for index in range(len(beats)) if index not in covered),
            key=lambda index: (beats[index].get("importance", 1), beats[index].get("keep", False),
                               beats[index].get("type") in {"COMBAT", "REACTION", "RESULT"}),
            reverse=True,
        )
        if not candidates:
            break
        index = candidates[0]
        scenes.append({"first_index": max(0, index - 1), "last_index": min(len(beats) - 1, index + 1),
                       "purpose": beats[index].get("reason", "high-value story moment"), "required": False})
        scenes = merge(scenes)

    if scenes:
        last_gameplay = max((index for index, beat in enumerate(beats) if beat.get("phase") == "IN_GAME"), default=None)
        final = scenes[-1]
        if last_gameplay is not None and final["last_index"] < last_gameplay:
            extended_duration = float(beats[last_gameplay]["end"]) - final["start"]
            if extended_duration <= 120:
                final.update(end_beat_id=beats[last_gameplay]["id"], last_index=last_gameplay, end=float(beats[last_gameplay]["end"]))
            else:
                first = last_gameplay
                while first > final["last_index"] + 1 and beats[last_gameplay]["end"] - beats[first - 1]["start"] <= 90:
                    first -= 1
                scenes.append({"start_beat_id": beats[first]["id"], "end_beat_id": beats[last_gameplay]["id"],
                               "first_index": first, "last_index": last_gameplay, "start": float(beats[first]["start"]),
                               "end": float(beats[last_gameplay]["end"]), "purpose": "actual final fight, result, and reaction",
                               "required": True})
        scenes[0]["required"] = True
        scenes[-1]["required"] = True
    scenes = merge(scenes)
    while sum(scene["end"] - scene["start"] for scene in scenes) > maximum:
        total = sum(scene["end"] - scene["start"] for scene in scenes)
        removable = [scene for scene in scenes[1:-1] if not scene.get("required")
                     and total - (scene["end"] - scene["start"]) >= 520]
        if not removable:
            break
        remove = min(removable, key=lambda scene: max(beats[index].get("importance", 1)
                                                       for index in range(scene["first_index"], scene["last_index"] + 1)))
        scenes.remove(remove)
    return scenes


def _even_frames(frames: list[dict], maximum: int = 18) -> list[dict]:
    if len(frames) <= maximum:
        return frames
    return [frames[round(index * (len(frames) - 1) / (maximum - 1))] for index in range(maximum)]


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
    if any(float(word.get("start", -1)) < end < float(word.get("end", -1)) for word in words):
        spanning = next(word for word in words if float(word.get("start", -1)) < end < float(word.get("end", -1)))
        end = min(region[1], float(spanning["end"]) + 0.1)
    return start, end


def _refine_long_scenes(vod_dir: Path, ai: AI, game: dict, beats: list[dict], scenes: list[dict], cache_key: dict,
                        cold_open_beat_id: int | None, force: bool) -> tuple[list[dict], dict]:
    source = Path(read_json(vod_dir / "manifest.json")["source"])
    words = transcript_words(vod_dir)
    path = vod_dir / "long-cuts.json"
    cached = read_json(path) if path.exists() else {}
    answers = cached.get("answers", {}) if not force and _cache_matches(cached, **cache_key, cuts_version=1) else {}
    reviewed_path = vod_dir / "long-cuts.reviewed.json"
    reviewed = read_json(reviewed_path) if reviewed_path.exists() else {}
    reviewed_answers = reviewed.get("answers", {}) if _cache_matches(reviewed, game_id=game["id"]) else {}
    cuts = []
    cold_open = None
    for scene in scenes:
        key = f"{scene['start_beat_id']}:{scene['end_beat_id']}"
        region = (max(-180, scene["start"] - 8), min(cache_key["editorial_end"] - game["gameplay_start"], scene["end"] + 12))
        frame_dir = vod_dir / "frames" / "long" / f"{game['id']}-{region[0]:.2f}-{region[1]:.2f}"
        sampled = sorted(frame_dir.glob("frame_*.jpg"))
        if not sampled:
            sampled = [Path(item["path"]) for item in sample_frames(
                source, frame_dir, 1, start=game["gameplay_start"] + region[0], end=game["gameplay_start"] + region[1]
            )]
        frames = [{"timestamp": region[0] + index, "path": str(frame)} for index, frame in enumerate(sampled)]
        preview = _even_frames(frames)
        nearby_beats = [beat for beat in beats if beat["start"] < region[1] and beat["end"] > region[0]]
        nearby_words = [word for word in words if region[0] <= float(word.get("start", -1)) < region[1]]
        is_cold = cold_open_beat_id is not None and scene["start_beat_id"] <= cold_open_beat_id <= scene["end_beat_id"]
        if key not in answers:
            answers[key] = ai.refine_long_scene(
                {**scene, "allowed_region": list(region)}, nearby_beats, nearby_words, preview, cold_open=is_cold
            )
            write_json(path, {**cache_key, "cuts_version": 1, "answers": answers})
        answer = reviewed_answers.get(key, answers[key])
        start, end = (answer.get("start"), answer.get("end")) if isinstance(answer, dict) else (None, None)
        if (type(start) not in (int, float) or type(end) not in (int, float) or not math.isfinite(start)
                or not math.isfinite(end) or not region[0] <= start < end <= region[1] or not 5 <= end - start <= 120):
            start, end = scene["start"], scene["end"]
        start, end = _avoid_split_word(start, end, nearby_words, region)
        cut = {"source_start": game["gameplay_start"] + start, "source_end": game["gameplay_start"] + end,
               "relative_start": start, "relative_end": end, "start_beat_id": scene["start_beat_id"],
               "end_beat_id": scene["end_beat_id"], "purpose": scene["purpose"],
               "raw_start": scene["start"], "raw_end": scene["end"]}
        cuts.append(cut)
        if is_cold and isinstance(answer, dict):
            hook_start, hook_end = answer.get("cold_open_start"), answer.get("cold_open_end")
            if (type(hook_start) in (int, float) and type(hook_end) in (int, float) and math.isfinite(hook_start)
                    and math.isfinite(hook_end) and start <= hook_start < hook_end <= end and 5 <= hook_end - hook_start <= 20):
                cold_open = {"source_start": game["gameplay_start"] + hook_start,
                             "source_end": game["gameplay_start"] + hook_end,
                             "relative_start": hook_start, "relative_end": hook_end}

    if cold_open is None:
        source_cut = next((cut for cut in cuts if cold_open_beat_id is not None
                           and cut["start_beat_id"] <= cold_open_beat_id <= cut["end_beat_id"]), cuts[-1])
        hook_start = source_cut["relative_start"]
        cold_open = {"source_start": game["gameplay_start"] + hook_start,
                     "source_end": game["gameplay_start"] + min(source_cut["relative_end"], hook_start + 7),
                     "relative_start": hook_start, "relative_end": min(source_cut["relative_end"], hook_start + 7)}
    return cuts, cold_open


def plan_long(vod_dir: Path, ai: AI, force: bool = False) -> dict:
    output = vod_dir / "long-plan.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    cache_key = {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end}
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key, long_version=14):
            return cached
    beats = _selected_beats(vod_dir, cache_key)
    proposal_path = vod_dir / "long-proposals.json"
    cached_proposal = read_json(proposal_path) if proposal_path.exists() else {}
    if not force and _cache_matches(cached_proposal, **cache_key, proposal_version=1):
        model_plan = cached_proposal["plan"]
    else:
        model_plan = ai.plan_long(beats)
        write_json(proposal_path, {**cache_key, "proposal_version": 1, "plan": model_plan})
    model_plan = {**model_plan, "scenes": [scene for scene in model_plan.get("scenes", [])
                                           if isinstance(scene, dict) and scene.get("start_beat_id", 0) >= 0]}
    pregame = [beat for beat in beats if beat.get("phase") == "CHAMP_SELECT" and beat.get("importance", 1) >= 3]
    if pregame:
        setup = pregame[-2:]
        model_plan["scenes"].append({"start_beat_id": setup[0]["id"], "end_beat_id": setup[-1]["id"],
                                     "purpose": "pick, build, and challenge premise", "required": True})
    scenes = _long_scene_ranges(beats, model_plan, target=530 if ai.mock else 740, maximum=690 if ai.mock else 780)
    segments, cold_open = _refine_long_scenes(
        vod_dir, ai, game, beats, scenes, cache_key, model_plan.get("cold_open_beat_id"), force
    )
    selected = [beat["id"] for scene in scenes for beat in beats[scene["first_index"] : scene["last_index"] + 1]]
    gameplay_duration = sum(segment["source_end"] - segment["source_start"] for segment in segments)
    title_duration, end_card_duration = 5.0, 12.0
    duration = gameplay_duration + cold_open["source_end"] - cold_open["source_start"] + title_duration + end_card_duration
    if not 540 <= duration <= 720:
        raise ValueError(f"Long edit duration {duration:.1f}s is outside 9-12 minutes")
    plan = {
        **cache_key, "long_version": 14,
        "story": model_plan.get("story", ""),
        "selected_ids_raw": model_plan.get("selected_ids", []),
        "selected_ids": selected,
        "gameplay_duration": gameplay_duration,
        "duration": duration,
        "cold_open": cold_open,
        "title_duration": title_duration,
        "end_card_duration": end_card_duration,
        "segments": segments,
    }
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
            math.log1p(math.hypot(sum(sample * cosine for sample, (cosine, _) in zip(chunk, wave_basis)),
                                  sum(sample * sine for sample, (_, sine) in zip(chunk, wave_basis))))
            for wave_basis in basis
        ])
    bounds = []
    for band in zip(*spectra):
        ordered = sorted(band)
        bounds.append((ordered[len(ordered) // 10], ordered[round(len(ordered) * 0.95) - 1]))
    result, previous = [], [0.0] * len(bounds)
    for spectrum in spectra:
        for index, (value, (floor, peak)) in enumerate(zip(spectrum, bounds)):
            normalized = min(1, max(0, (value - floor) / max(0.001, peak - floor))) ** 1.1
            blend = 0.65 if normalized > previous[index] else 0.3
            previous[index] += (normalized - previous[index]) * blend
        result.append([
            (previous[max(0, index - 1)] + 2 * value + previous[min(len(previous) - 1, index + 1)]) / 4
            for index, value in enumerate(previous)
        ])
    return result


def _title_frame(logo: Image.Image, time: float, spectrum: list[float] | None = None) -> Image.Image:
    background = "#060b18"
    frame = Image.new("RGBA", (1280, 720), background)
    if time < 0.18:
        return frame.convert("RGB")

    breath = 0.5 - 0.5 * math.cos(2 * math.pi * max(0, time - 0.5) / 2.1) if time >= 0.5 else 0
    recoil = _ease((time - 0.18) / 0.32)
    scale = 1.2 - 0.2 * recoil + 0.05 * breath
    supersample = 4
    icon = ImageOps.contain(logo, (260 * supersample, 260 * supersample), Image.Resampling.LANCZOS)
    icon = icon.resize((round(icon.width * scale), round(icon.height * scale)), Image.Resampling.LANCZOS)
    icon_layer = Image.new("RGBA", (340 * supersample, 340 * supersample))
    icon_layer.alpha_composite(icon, ((icon_layer.width - icon.width) // 2, (icon_layer.height - icon.height) // 2))
    icon_layer = icon_layer.resize((340, 340), Image.Resampling.LANCZOS)
    flash = Image.new("RGBA", frame.size)
    flash_alpha = round(120 * (1 - _ease((time - 0.18) / 0.18)))
    ImageDraw.Draw(flash).ellipse((500, 100, 780, 380), fill=(*ACCENT, flash_alpha))
    frame = Image.alpha_composite(frame, flash.filter(ImageFilter.GaussianBlur(45)))
    glow = Image.new("RGBA", frame.size)
    glow_expansion = round(18 * breath)
    ImageDraw.Draw(glow).ellipse((390 - glow_expansion, -20 - glow_expansion, 890 + glow_expansion, 500 + glow_expansion),
                                  fill=(*ACCENT, round(48 + 10 * breath)))
    frame = Image.alpha_composite(frame, glow.filter(ImageFilter.GaussianBlur(round(110 + 8 * breath))))
    spectrum = spectrum or [0.0] * 48
    energy = sum(spectrum) / len(spectrum)
    ring_scale, ring_size = 4, 440
    ring_points = []
    for index in range(192):
        angle = math.pi / 2 + 2 * math.pi * index / 192
        band_position = (index if index < 96 else 191 - index) * 47 / 95
        band = min(46, int(band_position))
        fraction = band_position - band
        level = spectrum[band] * (1 - fraction) + spectrum[band + 1] * fraction
        frequency_weight = 1.15 - 0.3 * band_position / 47
        bottom_clearance = 1 - 0.85 * max(0, math.sin(angle)) ** 6
        vibration = 26 * level ** 0.8 * frequency_weight * bottom_clearance
        radius = 167.2 + 3 * breath + vibration
        ring_points.append(((ring_size / 2 + math.cos(angle) * radius) * ring_scale,
                            (ring_size / 2 + math.sin(angle) * radius) * ring_scale))
    ring_points.append(ring_points[0])
    entrance = _ease((time - 0.18) / 0.25)
    ring = Image.new("RGBA", (ring_size * ring_scale, ring_size * ring_scale))
    ring_glow = Image.new("RGBA", ring.size)
    ImageDraw.Draw(ring_glow).line(ring_points, fill=(*ACCENT, round((25 + 20 * energy) * entrance)),
                                   width=8 * ring_scale, joint="curve")
    ring = Image.alpha_composite(ring, ring_glow.filter(ImageFilter.GaussianBlur(3 * ring_scale)))
    ImageDraw.Draw(ring).line(ring_points, fill=(*ACCENT, round((135 + 40 * energy) * entrance)),
                              width=3 * ring_scale, joint="curve")
    ring = ring.resize((ring_size, ring_size), Image.Resampling.LANCZOS)
    frame.alpha_composite(ring, (420, 50))
    frame.alpha_composite(icon_layer, (470, 100))

    text = Image.new("RGBA", frame.size)
    text_draw = ImageDraw.Draw(text)
    font = ImageFont.truetype(FILE, 108)
    label = "ElmilloR"
    box = text_draw.textbbox((0, 0), label, font=font, stroke_width=4)
    x = (1280 - box[2]) / 2
    y = 470
    shadow = Image.new("RGBA", frame.size)
    ImageDraw.Draw(shadow).text((x, y + 6), label, font=font, fill=(0, 0, 0, round(85 + 20 * breath)), stroke_width=4,
                                stroke_fill=(0, 0, 0, round(85 + 20 * breath)))
    frame = Image.alpha_composite(frame, shadow.filter(ImageFilter.GaussianBlur(round(8 + 2 * breath))))
    text_draw.text((x, y), label, font=font, fill=(*ACCENT, 255), stroke_width=4, stroke_fill=(5, 7, 6, 255))
    pass_progress = next((progress for start in (0.65, 2.65) if 0 < (progress := (time - start) / 0.75) < 1), -1)
    if 0 < pass_progress < 1:
        highlight = Image.new("RGBA", frame.size)
        ImageDraw.Draw(highlight).text((x, y), label, font=font, fill=(255, 252, 210, 210), stroke_width=4,
                                       stroke_fill=(*ACCENT, 180))
        center = x - 50 + (box[2] + 100) * _ease(pass_progress)
        band = Image.new("L", frame.size)
        ImageDraw.Draw(band).polygon(((center - 38, y + 140), (center + 2, y - 16),
                                     (center + 58, y - 16), (center + 18, y + 140)), fill=220)
        band = band.filter(ImageFilter.GaussianBlur(12))
        highlight.putalpha(ImageChops.multiply(highlight.getchannel("A"), band))
        text = Image.alpha_composite(text, highlight)
    frame = Image.alpha_composite(frame, text)
    if time > 4.70:
        fade = round(255 * (1 - _ease((time - 4.70) / 0.30)))
        frame.putalpha(fade)
        frame = Image.alpha_composite(Image.new("RGBA", frame.size, background), frame)
    return frame.convert("RGB")


def _render_title_card(path: Path, icon: Path, music: Path) -> None:
    with Image.open(icon) as source, tempfile.TemporaryDirectory() as temporary:
        logo = source.convert("RGBA")
        frames = Path(temporary)
        spectra = _audio_spectrum(music, 43.5)
        for index in range(150):
            _title_frame(logo, index / 30, spectra[index]).save(frames / f"frame-{index + 1:03d}.png")
        run_ffmpeg([
            "-framerate", "30", "-i", str(frames / "frame-%03d.png"),
            "-f", "lavfi", "-t", "5", "-i", "anullsrc=r=48000:cl=stereo",
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


def _long_cards() -> tuple[Path, Path, Path]:
    shared = Path("assets") / "long"
    icon = _download_asset(shared / "elmiillor-icon.png", "https://elmiillor.com/favicon.png")
    kick = _download_asset(shared / "kick-icon.ico", "https://kick.com/favicon.ico")
    x = _download_asset(shared / "x-icon.ico", "https://x.com/favicon.ico")
    music = _dream_eater(shared)
    source = shared / "dream-eater-source.json"
    source_metadata = {"name": "Dream Eater", "artist": "Hyleo", "collection": "Sessions: Star Guardian Taliyah",
                       "source": "https://www.youtube.com/watch?v=FDMYwUn5CIg", "title_clip_start": 42.5,
                       "end_clip_start": 42, "end_card_sync": 43}
    if not source.exists() or read_json(source) != source_metadata:
        write_json(source, source_metadata)
    title_path, end_path = shared / "title-card.mp4", shared / "end-card.mp4"
    if not title_path.exists() or abs(duration(title_path) - 5) > 0.1:
        _render_title_card(title_path, icon, music)
    if not end_path.exists() or abs(duration(end_path) - 12) > 0.1:
        _render_end_card(end_path, _trailer(shared), kick, x)
    return title_path, end_path, music


def render_long(vod_dir: Path) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    plan = read_json(vod_dir / "long-plan.json")
    output = vod_dir / "renders" / "long-rough.mp4"
    if plan.get("long_version") != 14 or "cold_open" not in plan:
        raise SystemExit("long-plan.json predates scene refinement; run plan-long first")
    title, end, music = _long_cards()
    render_long_video(Path(manifest["source"]), plan["segments"], plan["cold_open"], title, end, output,
                      title_duration=plan["title_duration"], end_duration=plan["end_card_duration"],
                      title_music=music, title_music_start=42.5, end_music=music, end_music_start=42)
    render_long_review(vod_dir)
    return output


def _long_transitions(plan: dict) -> list[dict]:
    cursor = plan["cold_open"]["source_end"] - plan["cold_open"]["source_start"]
    transitions = [{"output_time": cursor, "label": "cold open -> channel title"}]
    cursor += plan["title_duration"]
    transitions.append({"output_time": cursor, "label": "channel title -> premise"})
    for index, segment in enumerate(plan["segments"]):
        cursor += segment["source_end"] - segment["source_start"]
        label = "result -> end card" if index == len(plan["segments"]) - 1 else f"scene {index + 1} -> scene {index + 2}"
        transitions.append({"output_time": cursor, "label": label})
    return transitions


def render_long_review(vod_dir: Path) -> Path:
    plan = read_json(vod_dir / "long-plan.json")
    source = vod_dir / "renders" / "long-rough.mp4"
    if not source.exists():
        raise SystemExit("Render the long video before creating its transition review")
    transitions = _long_transitions(plan)
    write_json(vod_dir / "long-review.json", {"duration": plan["duration"], "transitions": transitions})
    intervals = [
        {"source_start": max(0, item["output_time"] - 2), "source_end": min(plan["duration"], item["output_time"] + 2)}
        for item in transitions
    ]
    output = vod_dir / "renders" / "long-transitions.mp4"
    render_segments(source, intervals, output)
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
    segments = answer.get("segments")
    first = segments[0] if isinstance(segments, list) and segments and isinstance(segments[0], dict) else {}
    last = segments[-1] if isinstance(segments, list) and segments and isinstance(segments[-1], dict) else {}
    hook = _resolve_evidence(answer.get("hook_evidence") or first.get("hook_evidence"), words, frames, *region)
    payoff = _resolve_evidence(answer.get("payoff_evidence") or last.get("payoff_evidence"), words, frames, *region, prefer_last=True)
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
    if not region[0] <= start < end <= region[1] or not 8 <= end - start <= 60:
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


def _retime_multi_short(candidate: dict, answer: object, words: list[dict], frames: list[dict], game_start: float) -> dict | None:
    if not isinstance(answer, dict) or answer.get("reject") or not all(
        isinstance(answer.get(key), str) and answer[key].strip() for key in ("title", "story", "hook", "payoff")
    ):
        return None
    proposed = candidate["segments"]
    segments = answer.get("segments")
    if not isinstance(segments, list) or len(segments) != len(proposed):
        return None
    trimmed = []
    for segment, bounds in zip(segments, proposed):
        if not isinstance(segment, dict) or type(segment.get("start")) not in (int, float) or type(segment.get("end")) not in (int, float):
            return None
        start, end = float(segment["start"]), float(segment["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < bounds["relative_start"] - 0.01 \
                or end > bounds["relative_end"] + 0.01 or end - start < 1:
            return None
        trimmed.append({"relative_start": start, "relative_end": end, "source_start": game_start + start, "source_end": game_start + end})
    total = sum(segment["relative_end"] - segment["relative_start"] for segment in trimmed)
    if not 8 <= total <= 60:
        return None
    hook = _resolve_evidence(answer.get("hook_evidence"), words, frames, trimmed[0]["relative_start"], trimmed[0]["relative_end"])
    payoff = _resolve_evidence(answer.get("payoff_evidence"), words, frames, trimmed[-1]["relative_start"], trimmed[-1]["relative_end"], prefer_last=True)
    if not hook or not payoff or not _short_evidence(hook[0], trimmed[0], words, frames, hook=True) \
            or not _short_evidence(payoff[0], trimmed[-1], words, frames, hook=False) or hook[0] == payoff[0]:
        return None
    return {
        **candidate, "segments": trimmed, "source_start": trimmed[0]["source_start"], "source_end": trimmed[-1]["source_end"],
        "relative_start": trimmed[0]["relative_start"], "relative_end": trimmed[-1]["relative_end"], "duration": total,
        "title": answer["title"][:100], "reason": answer["story"][:240], "hook": answer["hook"][:240],
        "payoff": answer["payoff"][:240], "hook_evidence": hook[0], "payoff_evidence": payoff[0],
    }


def _excluded(start: float, end: float, exclusions: list[dict]) -> bool:
    return any(start < item["end"] and end > item["start"] for item in exclusions)


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
        if _cache_matches(cached, **cache_key, evidence_version=3, cuts_version=5, output_version=10, callback_version=2):
            return cached["candidates"]
    beats = [beat for beat in _selected_beats(vod_dir, {
        "game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end,
        "transcript_edits": cache_key["transcript_edits"],
        "source_identity": source_identity,
    }) if beat.get("phase") != "CHAMP_SELECT"]
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
    game_duration = editorial_end - game["gameplay_start"]
    unhealthy_runs = [item for item in _unhealthy_transcript_ranges(vod_dir, beats, game_duration) if not _excluded(*item, exclusions)]
    visual_path = vod_dir / "short-visual-proposals.json"
    cached_visual = read_json(visual_path) if visual_path.exists() else {}
    if not force and _cache_matches(cached_visual, **cache_key):
        visual_proposals = cached_visual["candidates"]
    else:
        visual_proposals = []
        nearby = [frame for frame in stamped if 0 <= frame["timestamp"] < game_duration and not _excluded(frame["timestamp"], frame["timestamp"] + interval, exclusions)]
        scout = getattr(ai, "visual_shorts", lambda _frames: [])
        for offset in range(0, len(nearby), 10):
            batch = nearby[offset : offset + 12]
            if len(batch) < 3:
                continue
            for event in scout(batch):
                if not isinstance(event, dict) or any(type(event.get(key)) not in (int, float) or not math.isfinite(event[key]) for key in ("start", "end", "payoff")):
                    continue
                left, right, payoff = event["start"], event["end"], event["payoff"]
                if not (batch[0]["timestamp"] - interval <= left < payoff < right <= batch[-1]["timestamp"] + interval) or right - left > 60 or _excluded(left, right, exclusions):
                    continue
                scene_start = next((b["id"] for b in beats if b["end"] > left), None)
                scene_end = next((b["id"] for b in beats if b["end"] >= right), None)
                payoff_id = next((b["id"] for b in beats if b["end"] > payoff), None)
                if None not in (scene_start, scene_end, payoff_id):
                    visual_proposals.append({"title": str(event.get("description", "Visual play"))[:100], "proposed_payoff": str(event.get("description", ""))[:240],
                                             "payoff_beat_id": payoff_id, "scenes": [{"start_beat_id": scene_start, "end_beat_id": scene_end}], "origin": "visual"})
        write_json(visual_path, {**cache_key, "candidates": visual_proposals})
    callback_path = vod_dir / "short-callback-proposals.json"
    cached_callbacks = read_json(callback_path) if callback_path.exists() else {}
    if not force and _cache_matches(cached_callbacks, **cache_key, callback_version=2):
        callbacks = cached_callbacks["candidates"]
    else:
        callbacks = []
        callback_scout = getattr(ai, "callback_shorts", lambda _beats, _frames: [])
        for start, end in unhealthy_runs:
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
                callbacks.extend(callback_scout([*earlier, *late], batch)[:1])
        write_json(callback_path, {**cache_key, "callback_version": 2, "candidates": callbacks})
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
    proposals = list({json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in proposals if isinstance(item, dict)}.values())
    candidates = []
    rejected: list[dict] = []
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
            rejected.append({"title": str(proposal.get("title", "Untitled")), "stage": "verify",
                             "reason": str(verdict.get("reason", "Evidence gate rejected")) if isinstance(verdict, dict) else "Malformed verdict"})
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
        if len(trimmed) != len(ranges) or not 8 <= total <= 60:
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
        if not 8 <= total <= 60 or any(part["relative_end"] - part["relative_start"] < 1 for part in trimmed):
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
    gated = candidates
    if callable(getattr(ai, "refine_short", None)) and callable(getattr(ai, "audit_short", None)):
        cuts_path = vod_dir / "short-cuts.json"
        cached_cuts = read_json(cuts_path) if cuts_path.exists() else {}
        cuts = cached_cuts.get("answers", {}) if not force and _cache_matches(cached_cuts, **cache_key, cuts_version=5) else {}
        audits_path = vod_dir / "short-audits.json"
        cached_audits = read_json(audits_path) if audits_path.exists() else {}
        audits = cached_audits.get("answers", {}) if not force and _cache_matches(cached_audits, **cache_key, cuts_version=5, audit_version=4) else {}
        gated = []
        for candidate in candidates:
            cut = candidate
            if len(candidate["segments"]) == 1:
                region = (max(0, candidate["relative_start"] - 20), min(game_duration, candidate["relative_end"] + 45))
                frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{source_tag}-cuts-{region[0]:.2f}-{region[1]:.2f}"
                sampled = sorted(frame_dir.glob("frame_*.jpg"))
                if not sampled:
                    sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=game["gameplay_start"] + region[0], end=game["gameplay_start"] + region[1])]
                positions = {round(step * (len(sampled) - 1) / 21) for step in range(22)} if sampled else set()
                for evidence in (candidate["hook_evidence"], candidate["payoff_evidence"]):
                    center = round(evidence["time"] - region[0])
                    positions.update(range(max(0, center - 2), min(len(sampled), center + 3)))
                frames = [{"timestamp": region[0] + position, "path": str(sampled[position].resolve())} for position in sorted(positions)]
                relevant_words = [word for word in words if region[0] <= float(word.get("start", -1)) < region[1]]
                cut_key = _fingerprint({"candidate": {key: value for key, value in candidate.items() if key != "_images"}, "region": region})
                if cut_key not in cuts:
                    cuts[cut_key] = ai.refine_short(candidate, relevant_words, frames)
                    write_json(cuts_path, {**cache_key, "cuts_version": 5, "answers": cuts})
                cut = _retime_short(candidate, cuts[cut_key], relevant_words, frames, game["gameplay_start"], region)
                if cut is None:
                    answer = cuts[cut_key]
                    rejected.append({"title": candidate["title"], "stage": "refine", "reason": str(answer.get("reason", "Refinement rejected or could not evidence the same story")) if isinstance(answer, dict) else "Malformed refinement"})
                    continue
                cut["_images"] = [min(frames, key=lambda frame: abs(frame["timestamp"] - evidence["time"]))["path"]
                                  for evidence in (cut["hook_evidence"], cut["payoff_evidence"])]
            else:
                frames = []
                for segment in candidate["segments"]:
                    frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{source_tag}-refine-{segment['relative_start']:.2f}-{segment['relative_end']:.2f}"
                    sampled = sorted(frame_dir.glob("frame_*.jpg"))
                    if not sampled:
                        sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=segment["source_start"], end=segment["source_end"])]
                    positions = {round(step * (len(sampled) - 1) / 4) for step in range(5)} if sampled else set()
                    for evidence in (candidate["hook_evidence"], candidate["payoff_evidence"]):
                        if segment["relative_start"] <= evidence["time"] < segment["relative_end"]:
                            center = round(evidence["time"] - segment["relative_start"])
                            positions.update(range(max(0, center - 2), min(len(sampled), center + 3)))
                    frames.extend({"timestamp": segment["relative_start"] + position, "path": str(sampled[position].resolve())} for position in sorted(positions))
                relevant_words = [word for word in words if any(segment["relative_start"] <= float(word.get("start", -1)) < segment["relative_end"] for segment in candidate["segments"])]
                cut_key = _fingerprint({"candidate": {key: value for key, value in candidate.items() if key != "_images"}})
                if cut_key not in cuts:
                    cuts[cut_key] = ai.refine_short(candidate, relevant_words, frames)
                    write_json(cuts_path, {**cache_key, "cuts_version": 5, "answers": cuts})
                cut = _retime_multi_short(candidate, cuts[cut_key], relevant_words, frames, game["gameplay_start"])
                if cut is None:
                    answer = cuts[cut_key]
                    rejected.append({"title": candidate["title"], "stage": "refine", "reason": str(answer.get("reason", "Multi-scene refinement rejected or could not evidence the same story")) if isinstance(answer, dict) else "Malformed refinement"})
                    continue
                cut["_images"] = [min(frames, key=lambda frame: abs(frame["timestamp"] - evidence["time"]))["path"]
                                  for evidence in (cut["hook_evidence"], cut["payoff_evidence"])]
            preview = []
            for segment in cut["segments"]:
                frame_dir = vod_dir / "frames" / "shorts" / f"{game['id']}-{source_tag}-audit-{segment['relative_start']:.2f}-{segment['relative_end']:.2f}"
                sampled = sorted(frame_dir.glob("frame_*.jpg"))
                if not sampled:
                    sampled = [Path(item["path"]) for item in sample_frames(source, frame_dir, 1, start=segment["source_start"], end=segment["source_end"])]
                count = 5 if segment is cut["segments"][-1] else 2
                indices = sorted({round(step * (len(sampled) - 1) / max(1, count - 1)) for step in range(count)}) if sampled else []
                preview.extend({"timestamp": segment["relative_start"] + index, "path": str(sampled[index].resolve())} for index in indices)
            preview.extend(frame for frame in stamped if cut["segments"][-1]["relative_start"] <= frame["timestamp"] < cut["segments"][-1]["relative_end"]
                           and abs(frame["timestamp"] - cut["payoff_evidence"]["time"]) <= 5)
            preview.sort(key=lambda frame: frame["timestamp"])
            audit_key = _fingerprint({key: value for key, value in cut.items() if key != "_images"})
            if audit_key not in audits:
                cut_words = [word for word in words if any(segment["relative_start"] <= float(word["start"]) < segment["relative_end"] for segment in cut["segments"])]
                audits[audit_key] = ai.audit_short(cut, cut_words, preview) if preview else {"keep": False, "reason": "No audit frames"}
                write_json(audits_path, {**cache_key, "cuts_version": 5, "audit_version": 4, "answers": audits})
            audit = audits[audit_key]
            if not isinstance(audit, dict) or audit.get("keep") is not True:
                rejected.append({"title": cut["title"], "stage": "audit", "reason": str(audit.get("reason", "Quality gate rejected")) if isinstance(audit, dict) else "Malformed audit"})
                continue
            gated.append({**cut, "quality_class": str(audit.get("class", "unknown")), "gate_reason": str(audit.get("reason", ""))})

    choices_path = vod_dir / "short-choices.json"
    cached_choices = read_json(choices_path) if choices_path.exists() else {}
    candidate_signature = _fingerprint([{key: value for key, value in item.items() if key != "_images"} for item in gated])
    if not force and _cache_matches(cached_choices, **cache_key, candidate_signature=candidate_signature):
        chosen = cached_choices["selected_ids"]
    else:
        evidence = [{key: value for key, value in item.items() if key not in ("_images", "reason")} for item in gated]
        chosen = ai.choose_shorts(evidence, [Path(image) for item in gated for image in item["_images"]]) if gated else []
        write_json(choices_path, {**cache_key, "candidate_signature": candidate_signature, "selected_ids": chosen})
    if len(gated) == 1:
        chosen = [0]
    selected_ids = []
    for index in chosen if isinstance(chosen, list) else []:
        if type(index) is not int or not 0 <= index < len(gated) or index in selected_ids:
            continue
        candidate = gated[index]
        if any(
            sum(max(0, min(part["relative_end"], prior["relative_end"]) - max(part["relative_start"], prior["relative_start"]))
                for part in candidate["segments"] for prior in gated[previous]["segments"])
            / min(candidate["duration"], gated[previous]["duration"]) >= 0.5
            for previous in selected_ids
        ):
            continue
        selected_ids.append(index)
        if len(selected_ids) == 2:
            break
    selected = [
        {**{key: value for key, value in gated[index].items() if key != "_images"},
         "evidence_images": gated[index].get("_images", []),
         "output_number": output_number, "render_name": f"short-v8-{output_number:02d}"}
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
                                                     "gated_count": len(gated), "selected_count": len(selected), "rejected": rejected})
    write_json(output, {**cache_key, "evidence_version": 3, "cuts_version": 5, "output_version": 10, "callback_version": 2, "candidates": selected})
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
    render_name = candidate.get("render_name") or f"short-{candidate.get('output_number', candidate_index + 1):02d}"
    subtitles = _write_short_subtitles(vod_dir, {**candidate, "render_name": render_name})
    output = vod_dir / "renders" / f"{render_name}.mp4"
    output.with_suffix(".json").unlink(missing_ok=True)
    render_vertical(Path(manifest["source"]), candidate.get("segments") or [candidate],
                    layout["facecam"], layout.get("gameplay", [0.255, 0, 0.49, 1]), subtitles, output,
                    layout.get("hud"))
    expected_duration = sum(segment["source_end"] - segment["source_start"] for segment in candidate.get("segments") or [candidate])
    info = media_info(output)
    if abs(info["duration"] - expected_duration) > 0.5 or (info["width"], info["height"]) != (1080, 1920) or not info["audio"]:
        raise RuntimeError(f"Invalid short render {output}: expected {expected_duration:.2f}s 1080x1920 with audio, got {info}")
    source = Path(manifest["source"])
    source_stat = source.stat()
    write_json(output.with_suffix(".json"), {
        "render_version": 1,
        "candidate": _fingerprint(candidate),
        "captions": _file_fingerprint(subtitles),
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
