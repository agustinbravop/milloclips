from __future__ import annotations

import json
import math
from pathlib import Path

from .ai import AI
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
    return read_json(reviewed if reviewed.exists() else vod_dir / "short-candidates.json")["candidates"]


def transcript_words(vod_dir: Path) -> list[dict]:
    words = read_json(vod_dir / "transcript.json").get("words", [])
    review = vod_dir / "transcript.reviewed.json"
    if not review.exists():
        return words
    reviewed = read_json(review)
    ranges = reviewed.get("exclude_ranges", [])
    replacements = reviewed.get("replacements", [])
    return [
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


def find_shorts(vod_dir: Path, ai: AI, force: bool = False) -> list[dict]:
    output = vod_dir / "short-candidates.json"
    game = selected_game(vod_dir)
    editorial_end = game.get("result_end", game["gameplay_end"])
    cache_key = {"game_id": game["id"], "gameplay_start": game["gameplay_start"], "editorial_end": editorial_end}
    if output.exists() and not force:
        cached = read_json(output)
        if _cache_matches(cached, **cache_key):
            return cached["candidates"]
    beats = [beat for beat in _selected_beats(vod_dir, cache_key) if beat.get("phase") != "CHAMP_SELECT"]
    by_id = {beat["id"]: index for index, beat in enumerate(beats)}
    candidates = []
    score_names = ("hook", "context", "payoff", "tightness")
    for candidate in ai.shorts(beats):
        if not isinstance(candidate, dict):
            continue
        start_id, end_id = candidate.get("start_beat_id"), candidate.get("end_beat_id")
        if (
            not isinstance(start_id, int)
            or isinstance(start_id, bool)
            or not isinstance(end_id, int)
            or isinstance(end_id, bool)
            or start_id not in by_id
            or end_id not in by_id
        ):
            continue
        start_index, end_index = by_id[start_id], by_id[end_id]
        if end_index < start_index:
            continue
        start = max(0, float(beats[start_index]["start"]))
        end = min(editorial_end - game["gameplay_start"], float(beats[end_index]["end"]))
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        if end - start < 8:
            continue
        if end - start > 90:
            end = start + 90
        raw_scores = candidate.get("scores", {})
        fallback = candidate.get("score", 1)
        values = [raw_scores.get(name, fallback) if isinstance(raw_scores, dict) else fallback for name in score_names]
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
            continue
        scores = {name: max(1, min(5, float(value))) for name, value in zip(score_names, values)}
        candidates.append(
            {
                "source_start": game["gameplay_start"] + start,
                "source_end": game["gameplay_start"] + end,
                "relative_start": start,
                "relative_end": end,
                "start_beat_id": start_id,
                "end_beat_id": end_id,
                "title": str(candidate.get("title", "Untitled moment"))[:100],
                "reason": str(candidate.get("reason", ""))[:240],
                "scores": scores,
                "score": round(sum(scores.values()) / len(scores), 2),
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = []
    for candidate in candidates:
        if any(
            max(0, min(candidate["relative_end"], other["relative_end"]) - max(candidate["relative_start"], other["relative_start"]))
            / min(candidate["relative_end"] - candidate["relative_start"], other["relative_end"] - other["relative_start"])
            >= 0.5
            for other in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) == 10:
            break
    write_json(output, {**cache_key, "candidates": selected})
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


def _ass_text(words: list[dict], active: int) -> str:
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
    rendered = []
    for index, word in enumerate(escaped):
        separator = r"\N" if index == split else " " if index else ""
        colour = "&H000000FF&" if index == active else "&H00FFFFFF&"
        size = 80 if index == active else 72
        rendered.append(f"{separator}{{\\1c{colour}\\fs{size}}}{word}")
    return r"{\an5\pos(540,620)}" + "".join(rendered)


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
    review = vod_dir / "short-captions.reviewed.json"
    reviewed = read_json(review) if review.exists() else {}
    if reviewed.get("source_start") == candidate["source_start"] and reviewed.get("source_end") == candidate["source_end"]:
        subtitle_blocks = reviewed.get("blocks", [])
    else:
        words = [
            word
            for word in transcript_words(vod_dir)
            if float(word.get("start", 0)) < candidate["relative_end"] and float(word.get("end", 0)) >= candidate["relative_start"]
        ]
        subtitle_blocks = _subtitle_blocks(words, candidate)
    duration = candidate["source_end"] - candidate["source_start"]
    _validate_caption_blocks(subtitle_blocks, duration)
    events = []
    for block in subtitle_blocks:
        words = _caption_words(block)
        for index, word in enumerate(words):
            start = float(word["start"])
            end = float(words[index + 1]["start"]) if index + 1 < len(words) else float(block["end"])
            if end - start < 0.01:
                continue
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{_ass_text(words, index)}"
            )
    output = vod_dir / "short.ass"
    output.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Lato Heavy,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,60,60,0,1\n\n"
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
    output = vod_dir / "renders" / f"short-{candidate_index + 1:02d}.mp4"
    render_vertical(
        Path(manifest["source"]),
        candidate["source_start"],
        candidate["source_end"],
        layout["facecam"],
        layout.get("gameplay", [0.20, 0, 0.453, 1]),
        subtitles,
        output,
    )
    return output
