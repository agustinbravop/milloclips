from __future__ import annotations

import shutil
import os
from pathlib import Path

from .ai import AI
from .editing import (_cache_matches, _file_fingerprint, _fingerprint, _source_identity, _transcript_edits,
                      analyze, find_shorts, games, select_game, transcript_words)
from .io import read_json, write_json
from .media import sample_frames


def game_workspace(vod_dir: Path, game: dict) -> Path:
    """Seed the existing selected-game analysis once; leave its media caches intact."""
    output = vod_dir / "game-analyses" / game["id"]
    output.mkdir(parents=True, exist_ok=True)
    if not (output / "beats.json").exists() and (vod_dir / "beats.json").exists():
        old = read_json(vod_dir / "beats.json")
        if old.get("game_id") == game["id"]:
            for path in vod_dir.iterdir():
                if path.is_file() and path.suffix in {".json", ".ass", ".mp3"} and path.name not in {"games.json", "games.reviewed.json", "selection.json", "manifest.json"}:
                    shutil.copy2(path, output / path.name)
            for name in ("frames", "renders"):
                if (vod_dir / name).exists() and not (output / name).exists():
                    shutil.copytree(vod_dir / name, output / name, copy_function=os.link if name == "frames" else shutil.copy2)
    write_json(output / "manifest.json", read_json(vod_dir / "manifest.json"))
    write_json(output / "games.json", {"games": [game]})
    select_game(output, game["id"])
    return output


def rank_vod_games(vod_dir: Path, ai: AI, force: bool = False) -> tuple[list[dict], Path]:
    analyses = []
    for game in games(vod_dir):
        output = game_workspace(vod_dir, game)
        print(f"Analyzing {game['id']}", flush=True)
        beats = analyze(output, AI(output, model=ai.model, editorial_model=ai.editorial_model, mock=ai.mock), force=force)
        analyses.append({"game_id": game["id"], "duration": game["duration"], "beats": beats})
    signature = _fingerprint({"analyses": analyses, "model": ai.editorial_model, "ranking_version": 2})
    path = vod_dir / "game-ranking.json"
    cached = read_json(path) if path.exists() else {}
    ranking = cached["ranking"] if cached.get("signature") == signature and not force else ai.rank_games(analyses)
    write_json(path, {"signature": signature, "ranking": ranking})
    winner = ranking[0]["game_id"]
    select_game(vod_dir, winner)
    return ranking, vod_dir / "game-analyses" / winner


def _vod_regions(vod_dir: Path) -> list[dict]:
    """Cover every second, including games too short for the game detector."""
    total = float(read_json(vod_dir / "manifest.json")["duration"])
    regions = []
    cursor = 0.0
    for game in sorted(games(vod_dir), key=lambda item: item["gameplay_start"]):
        start, end = float(game["gameplay_start"]), float(game["gameplay_end"])
        if not 0 <= cursor <= start < end <= total:
            raise ValueError("Detected games must be bounded and non-overlapping")
        while cursor < start:
            right = min(start, cursor + 600)
            regions.append({"id": f"context-{len(regions):03d}", "gameplay_start": cursor, "gameplay_end": right,
                            "duration": right - cursor, "champ_select_start": None, "postgame_end": right})
            cursor = right
        regions.append(game)
        cursor = end
    while cursor < total:
        right = min(total, cursor + 600)
        regions.append({"id": f"context-{len(regions):03d}", "gameplay_start": cursor, "gameplay_end": right,
                        "duration": right - cursor, "champ_select_start": None, "postgame_end": right})
        cursor = right
    return regions


def _offset_event(value: object, origin: float) -> object:
    if isinstance(value, list):
        return [_offset_event(item, origin) for item in value]
    if not isinstance(value, dict):
        return value
    relative = {"start", "end", "relative_start", "relative_end", "time", "before_time", "action_start", "outcome_time", "reaction_end"}
    return {key: item + origin if key in relative and type(item) in (int, float) else _offset_event(item, origin)
            for key, item in value.items()}


def find_vod_shorts(vod_dir: Path, ai: AI, force: bool = False, *, output_dir: Path | None = None) -> list[dict]:
    """Assemble cached region evidence on a VOD clock, then judge all moments together."""
    output_dir = output_dir or vod_dir
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    output = vod_dir / "vod-shorts"
    output.mkdir(parents=True, exist_ok=True)
    total = float(manifest["duration"])
    game = {"id": "vod", "gameplay_start": 0.0, "gameplay_end": total, "result_end": total,
            "postgame_end": total, "champ_select_start": None, "duration": total,
            "league_ranges": [{"start": item.get("champ_select_start") or item["gameplay_start"],
                               "end": item.get("postgame_end", item["gameplay_end"])} for item in games(vod_dir)]}
    write_json(output / "manifest.json", manifest)
    write_json(output / "games.json", {"games": [game]})
    select_game(output, "vod")
    beats, words, frames, coverage, events = [], [], [], [], []
    for region in _vod_regions(vod_dir):
        directory = game_workspace(vod_dir, region)
        region_ai = AI(directory, model=ai.model, editorial_model=ai.editorial_model, mock=ai.mock)
        print(f"Whole-VOD evidence: {region['id']}", flush=True)
        local = analyze(directory, region_ai, force=force)
        origin, end = region["gameplay_start"], region["gameplay_end"]
        for beat in local:
            if beat["start"] < 0:
                continue
            beats.append({**beat, "id": len(beats), "start": origin + beat["start"], "end": min(end, origin + beat["end"]),
                          "source_region": region["id"]})
        words.extend({**word, "start": origin + word["start"], "end": min(end, origin + word["end"])}
                     for word in transcript_words(directory) if 0 <= word["start"] < end - origin)
        champ = region.get("champ_select_start")
        pregame = max(float(champ), origin - 180) if champ is not None and champ < origin else origin
        sampled = sample_frames(source, directory / "frames" / "selected-game", 5, start=pregame, end=end)
        frames.extend(frame for frame in sampled if origin <= frame["timestamp"] < end)
        coverage.append({"region": region["id"], "start": origin, "end": end,
                         "beats": _file_fingerprint(directory / "beats.json"), "transcript_edits": _transcript_edits(directory)})
        long_path = directory / "long-plan.json"
        plan = read_json(long_path) if long_path.exists() else {}
        if _cache_matches(plan, game_id=region["id"], gameplay_start=origin, source_identity=_source_identity(source),
                          beats=_file_fingerprint(directory / "beats.json"), transcript_edits=_transcript_edits(directory)):
            events.extend(_offset_event(plan.get("story_segments", plan.get("segments", [])), origin))
    if not coverage or coverage[0]["start"] != 0 or coverage[-1]["end"] != total:
        raise ValueError("Short scouting does not cover the entire VOD")
    cache_key = {"game_id": "vod", "gameplay_start": 0.0, "editorial_end": total,
                 "source_identity": _source_identity(source), "transcript_edits": _transcript_edits(output)}
    write_json(output / "transcript.json", {"words": sorted(words, key=lambda item: item["start"]), "coverage": coverage})
    write_json(output / "beats.json", {**cache_key, "target_beat_seconds": 15, "beats": beats, "coverage": coverage})
    write_json(output / "long-plan.json", {**cache_key, "beats": _file_fingerprint(output / "beats.json"), "story_segments": events})
    frame_dir = output / "frames" / "selected-game"
    frame_dir.mkdir(parents=True, exist_ok=True)
    write_json(frame_dir / "index.json", {"frames": sorted(frames, key=lambda item: item["timestamp"])})
    layout_region = games(vod_dir)[0] if games(vod_dir) else _vod_regions(vod_dir)[0]
    layout_dir = vod_dir / "game-analyses" / layout_region["id"]
    write_json(output / "layout.json", read_json(layout_dir / "layout.json"))
    vod_ai = AI(output, model=ai.model, editorial_model=ai.editorial_model, mock=ai.mock)
    candidates = find_shorts(output, vod_ai, force=force)
    if len(candidates) != 2:
        raise ValueError(f"Whole-VOD selection produced {len(candidates)} shorts; two are required")
    write_json(output_dir / "short-candidates.json", {"scope": "vod", "workspace": str(output.resolve()),
               "signature": _file_fingerprint(output / "short-candidates.json")})
    for candidate in candidates:
        name = candidate["render_name"]
        shutil.copy2(output / f"{name}.captions.raw.json", output_dir / f"{name}.captions.raw.json")
    return candidates
