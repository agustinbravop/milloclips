from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
from pathlib import Path

import imageio_ffmpeg
from yt_dlp import YoutubeDL

from .io import write_json


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ffmpeg(), "-hide_banner", "-y", *arguments],
        check=True,
        text=True,
        capture_output=capture,
    )


def duration(path: Path) -> float:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path)],
        text=True,
        capture_output=True,
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"Could not determine media duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ingest(source: str, root: Path) -> Path:
    vod_id = hashlib.sha256(source.encode()).hexdigest()[:12]
    vod_dir = root / vod_id
    vod_dir.mkdir(parents=True, exist_ok=True)

    local = Path(source).expanduser()
    if local.exists():
        source_path = local.resolve()
        metadata = {"kind": "local", "input": str(source_path)}
    else:
        template = str(vod_dir / "source.%(ext)s")
        with YoutubeDL(
            {
                # ponytail: 720p caps iteration cost; lift for production masters.
                "format": "best[height<=720]/best",
                "merge_output_format": "mp4",
                "outtmpl": template,
                "noplaylist": True,
                "quiet": False,
            }
        ) as downloader:
            info = downloader.extract_info(source, download=True)
            metadata = {
                "kind": "kick",
                "input": source,
                "title": info.get("title"),
                "id": info.get("id"),
                "timestamp": info.get("timestamp"),
            }
        candidates = sorted(vod_dir.glob("source.*"))
        if not candidates:
            raise RuntimeError("yt-dlp finished without creating source media")
        source_path = candidates[0].resolve()

    manifest = {
        "vod_id": vod_id,
        "source": str(source_path),
        "duration": duration(source_path),
        "metadata": metadata,
    }
    write_json(vod_dir / "manifest.json", manifest)
    return vod_dir


def sample_frames(
    source: Path,
    output: Path,
    interval: float,
    *,
    start: float = 0,
    end: float | None = None,
    width: int = 960,
) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    for old in output.glob("*.jpg"):
        old.unlink()
    arguments = ["-ss", str(start), "-i", str(source)]
    if end is not None:
        arguments += ["-t", str(max(0, end - start))]
    arguments += [
        "-vf",
        f"fps=1/{interval},scale={width}:-2",
        "-q:v",
        "4",
        "-start_number",
        "0",
        str(output / "frame_%06d.jpg"),
    ]
    run_ffmpeg(arguments)
    return [
        {"timestamp": start + index * interval, "path": str(path.resolve())}
        for index, path in enumerate(sorted(output.glob("frame_*.jpg")))
    ]


def extract_audio(source: Path, output: Path, start: float, end: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(end - start),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(output),
        ]
    )


def render_segments(source: Path, segments: list[dict], output: Path) -> None:
    if not segments:
        raise ValueError("Cannot render an empty plan")
    output.parent.mkdir(parents=True, exist_ok=True)
    chains: list[str] = []
    inputs: list[str] = []
    for index, segment in enumerate(segments):
        start, end = segment["source_start"], segment["source_end"]
        chains += [
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]",
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]",
        ]
        inputs.append(f"[v{index}][a{index}]")
    chains.append(f"{''.join(inputs)}concat=n={len(segments)}:v=1:a=1[rawv][rawa]")
    chains.append("[rawv]fps=30[v]")
    chains.append("[rawa]loudnorm,aresample=48000[a]")
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def render_vertical(
    source: Path,
    segments: list[dict],
    facecam: list[float],
    gameplay: list[float],
    subtitles: Path,
    output: Path,
    hud: list[float] | None = None,
) -> None:
    if not segments or any(not all(math.isfinite(segment[key]) for key in ("source_start", "source_end"))
                           or not 0 <= segment["source_start"] < segment["source_end"] for segment in segments) or any(
        right["source_start"] < left["source_end"] for left, right in zip(segments, segments[1:])
    ):
        raise ValueError("Short segments must be chronological, non-overlapping positive intervals")
    output.parent.mkdir(parents=True, exist_ok=True)
    hud = [0.855, 0, 0.145, 0.028] if hud is None else hud
    for name, crop in (("Facecam", facecam), ("Gameplay", gameplay), ("HUD", hud)):
        if len(crop) != 4:
            raise ValueError(f"{name} crop must contain x, y, width, and height")
        x, y, width, height = crop
        if not all(math.isfinite(value) for value in crop) or width <= 0 or height <= 0 or min(x, y) < 0 or x + width > 1 or y + height > 1:
            raise ValueError(f"Invalid normalized {name.lower()} crop: {crop}")
    face_x, face_y, face_width, face_height = facecam
    game_x, game_y, game_width, game_height = gameplay
    hud_x, hud_y, hud_width, hud_height = hud
    face = (
        f"crop=iw*{face_width}:ih*{face_height}:iw*{face_x}:ih*{face_y},"
        "scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680,setsar=1"
    )
    game = (
        f"crop=iw*{game_width}:ih*{game_height}:iw*{game_x}:ih*{game_y},"
        "scale=1080:1240:force_original_aspect_ratio=increase,crop=1080:1240,setsar=1"
    )
    score = (
        f"crop=iw*{hud_width}:ih*{hud_height}:iw*{hud_x}:ih*{hud_y},"
        "scale=320:-2:flags=lanczos,setsar=1"
    )
    escaped_subtitles = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts = Path(__file__).with_name("fonts").resolve()
    chains = []
    inputs = []
    for index, segment in enumerate(segments):
        chains.extend((f"[{index}:v]setpts=PTS-STARTPTS[v{index}]",
                       f"[{index}:a]asetpts=N/SR/TB[a{index}]"))
        inputs.append(f"[v{index}][a{index}]")
    chains.extend((f"{''.join(inputs)}concat=n={len(segments)}:v=1:a=1[rawv][rawa]",
                   "[rawv]split=3[face][game][hud]", f"[face]{face}[faceout]", f"[game]{game}[gameout]",
                    f"[hud]{score}[hudout]", "[gameout][hudout]overlay=x=W-w:y=0[gamehud]",
                   f"[faceout][gamehud]vstack=inputs=2,ass='{escaped_subtitles}':fontsdir='{fonts}',fps=30[v]",
                   "[rawa]aresample=48000[a]"))
    run_ffmpeg(
        [
            *[argument for segment in segments for argument in (
                "-ss", str(segment["source_start"]), "-t", str(segment["source_end"] - segment["source_start"]),
                "-i", str(source),
            )],
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ]
    )
