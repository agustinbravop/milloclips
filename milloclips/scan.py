from __future__ import annotations

from pathlib import Path

from .ai import AI
from .io import read_json, write_json
from .media import sample_frames


GAME_STATES = {"CHAMP_SELECT", "LOADING", "IN_GAME", "POST_GAME"}
MIN_GAME_DURATION = 540


def _runs(points: list[dict], total_duration: float) -> list[dict]:
    if not points:
        return []
    runs: list[dict] = []
    for point in sorted(points, key=lambda item: item["timestamp"]):
        if runs and runs[-1]["state"] == point["state"]:
            runs[-1]["end"] = point["timestamp"]
            runs[-1]["points"].append(point)
        else:
            runs.append(
                {
                    "state": point["state"],
                    "start": point["timestamp"],
                    "end": point["timestamp"],
                    "points": [point],
                }
            )
    for index, run in enumerate(runs):
        run["end"] = runs[index + 1]["start"] if index + 1 < len(runs) else total_duration
    return runs


def detect_games(points: list[dict], total_duration: float) -> list[dict]:
    runs = _runs(points, total_duration)
    gameplay = [run.copy() for run in runs if run["state"] == "IN_GAME"]
    merged: list[dict] = []
    for run in gameplay:
        if merged and run["start"] - merged[-1]["end"] <= 90:
            merged[-1]["end"] = run["end"]
        else:
            merged.append(run)

    games = []
    for index, game in enumerate(merged, start=1):
        if game["end"] - game["start"] < MIN_GAME_DURATION:
            continue
        before = [run for run in runs if run["end"] <= game["start"] and game["start"] - run["end"] <= 1200]
        loading = next((run for run in reversed(before) if run["state"] == "LOADING"), None)
        champ = next((run for run in reversed(before) if run["state"] == "CHAMP_SELECT"), None)
        after = next((run for run in runs if run["start"] >= game["end"] and run["state"] == "POST_GAME"), None)
        games.append(
            {
                "id": f"game-{len(games) + 1:03d}",
                "champ_select_start": champ["start"] if champ else None,
                "loading_start": loading["start"] if loading else None,
                "gameplay_start": game["start"],
                "gameplay_end": game["end"],
                "postgame_end": min(after["end"], game["end"] + 90) if after else game["end"],
                "duration": game["end"] - game["start"],
            }
        )
    return games


def scan(vod_dir: Path, ai: AI, force: bool = False, coarse_interval: float = 30, refine_interval: float = 5) -> list[dict]:
    output = vod_dir / "games.json"
    if output.exists() and not force:
        return read_json(output)["games"]
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    coarse_frames = sample_frames(source, vod_dir / "frames" / "coarse", coarse_interval, end=manifest["duration"], force=force)
    coarse = ai.classify_frames(coarse_frames, manifest["duration"])

    windows: list[tuple[float, float]] = []
    for left, right in zip(coarse, coarse[1:]):
        if left["state"] != right["state"] and ({left["state"], right["state"]} & GAME_STATES):
            windows.append((max(0, right["timestamp"] - coarse_interval), min(manifest["duration"], right["timestamp"] + coarse_interval)))

    merged_windows: list[tuple[float, float]] = []
    for start, end in windows:
        if merged_windows and start <= merged_windows[-1][1]:
            merged_windows[-1] = (merged_windows[-1][0], max(end, merged_windows[-1][1]))
        else:
            merged_windows.append((start, end))
    windows = merged_windows

    refined: list[dict] = []
    for index, (start, end) in enumerate(windows):
        frames = sample_frames(source, vod_dir / "frames" / f"refined-{index:03d}", refine_interval, start=start, end=end, force=force)
        refined.extend(ai.classify_frames(frames, manifest["duration"]))

    for start, end in windows:
        coarse = [point for point in coarse if not start <= point["timestamp"] <= end]
    points = sorted(coarse + refined, key=lambda item: item["timestamp"])
    games = detect_games(points, manifest["duration"])
    write_json(vod_dir / "stream-states.json", {"points": points})
    write_json(output, {"games": games})
    return games
