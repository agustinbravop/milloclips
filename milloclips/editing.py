from __future__ import annotations

import json
from pathlib import Path

from .ai import AI
from .io import read_json, write_json
from .media import extract_audio, render_segments, render_vertical, sample_frames


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
    ranges = read_json(review).get("exclude_ranges", [])
    return [
        word
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
    if output.exists() and not force:
        return read_json(output)["beats"]
    manifest = read_json(vod_dir / "manifest.json")
    game = selected_game(vod_dir)
    source = Path(manifest["source"])
    editorial_end = game.get("result_end", game["gameplay_end"])
    game_duration = editorial_end - game["gameplay_start"]
    champ_select_start = game.get("champ_select_start")
    pregame_start = (
        max(float(champ_select_start), game["gameplay_start"] - 180)
        if champ_select_start is not None and champ_select_start < game["gameplay_start"]
        else game["gameplay_start"]
    )
    pregame_duration = game["gameplay_start"] - pregame_start

    transcript_path = vod_dir / "transcript.json"
    if transcript_path.exists():
        transcript = read_json(transcript_path)
    else:
        audio = vod_dir / "selected-game.mp3"
        extract_audio(source, audio, game["gameplay_start"], editorial_end)
        transcript = ai.transcribe(audio, game_duration)
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
        if pregame_path.exists():
            pregame = read_json(pregame_path)
        else:
            audio = vod_dir / "selected-pregame.mp3"
            extract_audio(source, audio, pregame_start, game["gameplay_start"])
            pregame = ai.transcribe(audio, pregame_duration)
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
    write_json(output, {"game_id": game["id"], "target_beat_seconds": beat_seconds, "beats": classified})
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
    if output.exists() and not force:
        return read_json(output)
    beats = read_json(vod_dir / "beats.json")["beats"]
    game = selected_game(vod_dir)
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
        "game_id": game["id"],
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
    if output.exists() and not force:
        return read_json(output)["candidates"]
    beats = read_json(vod_dir / "beats.json")["beats"]
    game = selected_game(vod_dir)
    candidates = []
    for candidate in ai.shorts([beat for beat in beats if beat.get("phase") != "CHAMP_SELECT"]):
        start = max(0, float(candidate.get("start", 0)))
        end = min(
            game.get("result_end", game["gameplay_end"]) - game["gameplay_start"],
            float(candidate.get("end", start + 30)),
        )
        if end - start < 8:
            continue
        if end - start > 90:
            end = start + 90
        candidates.append(
            {
                "source_start": game["gameplay_start"] + start,
                "source_end": game["gameplay_start"] + end,
                "relative_start": start,
                "relative_end": end,
                "title": str(candidate.get("title", "Untitled moment"))[:100],
                "reason": str(candidate.get("reason", ""))[:240],
                "score": max(1, min(5, int(candidate.get("score", 1)))),
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    write_json(output, {"game_id": game["id"], "candidates": candidates})
    return candidates


def _srt_time(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


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
    blocks = [
        f"{index}\n{_srt_time(block['start'])} --> {_srt_time(block['end'])}\n{block['text']}\n"
        for index, block in enumerate(subtitle_blocks, start=1)
    ]
    output = vod_dir / "short.srt"
    output.write_text("\n".join(blocks), encoding="utf-8")
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
            float(group[-1]["end"]) - candidate["relative_start"],
        )
        text = " ".join(str(word["word"]).strip() for word in group)
        if blocks and blocks[-1]["end"] > start:
            blocks[-1]["end"] = start
        blocks.append({"start": start, "end": max(start + 0.1, end), "text": text})
        group.clear()

    for word in words:
        text = str(word.get("word", "")).strip()
        if not text:
            continue
        proposed = " ".join([*(str(item["word"]).strip() for item in group), text])
        if group and (
            float(word["start"]) - float(group[-1]["end"]) > 0.65
            or len(group) >= 4
            or len(proposed) > 28
            or float(word["end"]) - float(group[0]["start"]) > 2.5
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
    if candidate_index >= len(candidates):
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
