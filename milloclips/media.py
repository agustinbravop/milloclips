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


def media_info(path: Path) -> dict:
    result = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], text=True, capture_output=True)
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    video_match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", result.stderr)
    if not duration_match or not video_match:
        raise RuntimeError(f"Could not validate rendered media: {path}")
    hours, minutes, seconds = duration_match.groups()
    return {
        "duration": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        "width": int(video_match.group(1)),
        "height": int(video_match.group(2)),
        "audio": "Audio:" in result.stderr,
    }


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
                "format": "bestvideo+bestaudio/best",
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


def title_music_window(cold_open_duration: float, title_duration: float, *, lead: float = 1.0, tail: float = 1.0) -> dict:
    """Place a music clip so it fades in before the title card and out after it."""
    if title_duration <= 0 or lead < 0 or tail < 0 or cold_open_duration < lead:
        raise ValueError("Title music window does not fit around the title card")
    return {
        "delay": cold_open_duration - lead,
        "length": lead + title_duration + tail,
        "fade_in": lead,
        "fade_out": tail,
    }


def render_long_video(
    source: Path,
    segments: list[dict],
    cold_open: dict,
    title_card: Path,
    end_card: Path,
    output: Path,
    *,
    title_duration: float = 2.5,
    end_duration: float = 8.0,
    title_music: Path | None = None,
    title_music_start: float = 0.0,
    title_music_volume: float = 0.309375,
    end_music: Path | None = None,
    end_music_start: float = 0.0,
    end_music_volume: float = 0.55,
) -> None:
    if not segments or not 0 <= cold_open["source_start"] < cold_open["source_end"]:
        raise ValueError("Long video requires gameplay scenes and a valid cold open")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_parts = [cold_open, *segments]
    chains: list[str] = []
    for index, segment in enumerate(source_parts):
        chains.extend((
            f"[{index}:v]setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,fps=30[v{index}]",
            f"[{index}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[a{index}]",
        ))
    title_input = len(source_parts)
    end_input = title_input + 1
    chains.extend((
        f"[{title_input}:v]trim=duration={title_duration},setpts=PTS-STARTPTS,fps=30[titlev]",
        f"[{title_input}:a]atrim=duration={title_duration},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[titlea]",
        f"[{end_input}:v]trim=duration={end_duration},setpts=PTS-STARTPTS,fps=30[endv]",
        f"[{end_input}:a]atrim=duration={end_duration},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume=0[enda]",
    ))
    ordered = ["[v0][a0]", "[titlev][titlea]", *[f"[v{index}][a{index}]" for index in range(1, len(source_parts))], "[endv][enda]"]
    chains.extend((
        f"{''.join(ordered)}concat=n={len(ordered)}:v=1:a=1[rawv][rawa]",
        "[rawv]fps=30[v]",
        "[rawa]loudnorm,aresample=48000[proga]",
    ))
    music_inputs: list[str] = []
    music_labels: list[str] = []
    next_input = end_input + 1
    if title_music is not None:
        window = title_music_window(cold_open["source_end"] - cold_open["source_start"], title_duration)
        music_inputs += ["-ss", str(title_music_start), "-t", str(window["length"]), "-i", str(title_music)]
        chains.append(
            f"[{next_input}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={title_music_volume},"
            f"afade=t=in:st=0:d={window['fade_in']}:curve=qua,"
            f"afade=t=out:st={window['length'] - window['fade_out']}:d={window['fade_out']}:curve=qua,"
            f"adelay={round(window['delay'] * 1000)}:all=1[musica]"
        )
        music_labels.append("[musica]")
        next_input += 1
    if end_music is not None:
        lead = 1.0
        length = lead + end_duration
        delay = (cold_open["source_end"] - cold_open["source_start"] + title_duration
                 + sum(segment["source_end"] - segment["source_start"] for segment in segments) - lead)
        if delay < 0:
            raise ValueError("End music lead does not fit before the end card")
        music_inputs += ["-ss", str(end_music_start), "-t", str(length), "-i", str(end_music)]
        chains.append(
            f"[{next_input}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={end_music_volume},"
            f"afade=t=in:st=0:d={lead}:curve=qua,afade=t=out:st={length - 1}:d=1:curve=qua,"
            f"adelay={round(delay * 1000)}:all=1[endmusic]"
        )
        music_labels.append("[endmusic]")
    if music_labels:
        chains.append(f"[proga]{''.join(music_labels)}amix=inputs={len(music_labels) + 1}:duration=first:dropout_transition=0:normalize=0[a]")
    else:
        chains.append("[proga]anull[a]")
    run_ffmpeg([
        *[argument for segment in source_parts for argument in (
            "-ss", str(segment["source_start"]), "-t", str(segment["source_end"] - segment["source_start"]),
            "-i", str(source),
        )],
        "-t", str(title_duration), "-i", str(title_card),
        "-t", str(end_duration), "-i", str(end_card),
        *music_inputs,
        "-filter_complex", ";".join(chains),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-movflags", "+faststart", str(output),
    ])


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
                       f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000:first_pts=0[a{index}]"))
        inputs.append(f"[v{index}][a{index}]")
    chains.extend((f"{''.join(inputs)}concat=n={len(segments)}:v=1:a=1[rawv][rawa]",
                   "[rawv]split=3[face][game][hud]", f"[face]{face}[faceout]", f"[game]{game}[gameout]",
                    f"[hud]{score}[hudout]", "[gameout][hudout]overlay=x=W-w:y=0[gamehud]",
                   f"[faceout][gamehud]vstack=inputs=2,ass='{escaped_subtitles}':fontsdir='{fonts}',fps=30[v]",
                    "[rawa]loudnorm,aresample=48000[a]"))
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
