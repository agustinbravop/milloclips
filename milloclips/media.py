from __future__ import annotations

import hashlib
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
    start: float,
    end: float,
    facecam: list[float],
    subtitles: Path,
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(facecam) != 4:
        raise ValueError("Facecam crop must contain x, y, width, and height")
    x, y, width, height = facecam
    if width <= 0 or height <= 0 or min(x, y) < 0 or x + width > 1 or y + height > 1:
        raise ValueError(f"Invalid normalized facecam crop: {facecam}")
    face = (
        f"crop=iw*{width}:ih*{height}:iw*{x}:ih*{y},"
        "scale=1080:700:force_original_aspect_ratio=decrease,"
        "pad=1080:700:(ow-iw)/2:(oh-ih)/2:black"
    )
    game = "crop=iw*0.50:ih:iw*0.15:0,scale=1080:1220"
    escaped_subtitles = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:")
    filters = (
        f"[0:v]split=2[face][game];[face]{face}[faceout];[game]{game}[gameout];"
        f"[faceout][gameout]vstack=inputs=2,subtitles='{escaped_subtitles}':"
        "force_style='FontName=DejaVu Sans,FontSize=10,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
        "Shadow=0,Alignment=2,MarginV=45',fps=30[v]"
    )
    run_ffmpeg(
        [
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(end - start),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "0:a",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-af",
            "loudnorm,aresample=48000",
            "-shortest",
            str(output),
        ]
    )
