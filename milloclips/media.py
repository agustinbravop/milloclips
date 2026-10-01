from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import imageio_ffmpeg
from yt_dlp import YoutubeDL

from .io import read_json, write_json


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


def _kick_recording_url(source: str) -> str | None:
    if not source.startswith("https://kick.com/"):
        return None
    with urlopen(Request(source, headers={"User-Agent": "milloclips/0.1"}), timeout=20) as response:
        page = response.read().decode("utf-8")
    match = re.search(r'(?:\\")?recording_url(?:\\")?:(?:\\")?(https:[^"\\]+)', page)
    return match.group(1).replace("\\/", "/") if match else None


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
        download_source = _kick_recording_url(source) or source
        with YoutubeDL(
            {
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "outtmpl": template,
                "noplaylist": True,
                "quiet": False,
            }
        ) as downloader:
            info = downloader.extract_info(download_source, download=True)
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
    force: bool = False,
) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    existing = sorted(output.glob("frame_*.jpg"))
    if force or end is None:
        for old in existing:
            old.unlink()
        existing = []
    resume_at = start + len(existing) * interval
    if end is not None and resume_at < end:
        arguments = ["-ss", str(resume_at), "-i", str(source), "-t", str(max(0, end - resume_at))]
        arguments += [
            "-vf",
            f"fps=1/{interval},scale={width}:-2",
            "-q:v",
            "4",
            "-start_number",
            str(len(existing)),
            str(output / "frame_%06d.jpg"),
        ]
        run_ffmpeg(arguments)
    return [
        {"timestamp": start + index * interval, "path": str(path.resolve())}
        for index, path in enumerate(sorted(output.glob("frame_*.jpg")))
    ]


def sample_boundary_frames(source: Path, output: Path, interval: float, *, start: float, end: float, width: int = 960) -> list[dict]:
    """Cache decoded frame times on the same source-relative clock as input -ss.

    Unlike fps resampling, select preserves the decoded frame's presentation time.
    FFmpeg normalizes input start_time (including MPEG-TS offsets) before filtering.
    """
    if not all(math.isfinite(value) for value in (start, end, interval)) or not 0 <= start < end or interval <= 0:
        raise ValueError("Boundary sampling requires finite positive bounds and interval")
    stat = source.stat()
    key = {"version": 2, "source": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
           "start": start, "end": end, "interval": interval, "width": width}
    manifest = output / "frames.json"
    if manifest.exists():
        cached = read_json(manifest)
        if cached.get("sampling") == key and all(Path(frame["path"]).exists() for frame in cached.get("frames", [])):
            return cached["frames"]
    output.mkdir(parents=True, exist_ok=True)
    for old in output.glob("frame_*.jpg"):
        old.unlink()
    # ponytail: 5s covers current VOD GOPs; increase if missing reference frames persist.
    seek = max(0, start - 5)
    result = run_ffmpeg([
        "-ss", str(seek), "-t", str(end - seek + 1), "-i", str(source),
        "-vf", f"trim=start={start - seek}:end={end - seek},select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval})',"
               f"scale={width}:-2,showinfo,setpts=PTS-STARTPTS",
        "-fps_mode", "vfr", "-q:v", "4", "-start_number", "0", str(output / "frame_%06d.jpg"),
    ], capture=True)
    times = [float(value) for value in re.findall(r"\bpts_time:([-+\d.eE]+)", result.stderr)]
    paths = sorted(output.glob("frame_*.jpg"))
    if not paths or len(paths) != len(times):
        raise ValueError("Decoded boundary frame timestamps do not match extracted images")
    frames = [{"timestamp": seek + time, "path": str(path.resolve())} for time, path in zip(times, paths)]
    if any(not start - 0.001 <= frame["timestamp"] < end + 0.001 for frame in frames):
        raise ValueError("Decoded boundary frame lies outside its source window")
    if frames[0]["timestamp"] - start > max(interval, 1 / 30) + 0.05:
        raise ValueError("Decoder missed the beginning of the boundary window; more pre-roll is needed")
    write_json(manifest, {"sampling": key, "frames": frames})
    return frames


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


TITLE_DISSOLVE = 0.5
TITLE_DURATION = 7.0
TITLE_MUSIC_START = 42.5
TITLE_MUSIC_LEAD = 1.0


def intro_duration(intro: dict) -> float:
    return sum(part["source_end"] - part["source_start"] for part in intro.get("segments") or [intro])


def title_start(intro_duration: float, title_duration: float, dissolve: float = TITLE_DISSOLVE) -> float:
    if not all(math.isfinite(value) for value in (intro_duration, title_duration, dissolve)) or not 0 <= dissolve < min(intro_duration, title_duration):
        raise ValueError("Title dissolve must fit inside the intro and card")
    return intro_duration - dissolve


def title_music_window(cold_open_duration: float, title_duration: float, *, lead: float = TITLE_MUSIC_LEAD, tail: float = 1.0,
                       dissolve: float = TITLE_DISSOLVE) -> dict:
    """Place a music clip so it fades in before the title card and out after it."""
    card_start = title_start(cold_open_duration, title_duration, dissolve)
    if lead < 0 or tail < 0 or card_start < lead or title_duration + tail < 1:
        raise ValueError("Title music window does not fit around the title card")
    return {
        "delay": card_start - lead,
        "length": lead + title_duration + tail,
        "fade_in": lead + 1.0,
        "fade_out": tail,
    }


def _segment_input(source: Path, segment: dict) -> tuple[list[str], float, float]:
    start, end = segment["source_start"], segment["source_end"]
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (start, end)) or not 0 <= start < end:
        raise ValueError("Source segments must have finite positive bounds")
    seek = max(0, start - 5)
    return ["-ss", str(seek), "-t", str(end - seek + 1), "-i", str(source)], start - seek, end - seek


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
    title_transition: float = TITLE_DISSOLVE,
    title_music: Path | None = None,
    title_music_start: float = 0.0,
    title_music_volume: float = 0.07734375,
    end_music: Path | None = None,
    end_music_start: float = 0.0,
    end_music_volume: float = 0.103125,
) -> None:
    if not segments or not 0 <= cold_open["source_start"] < cold_open["source_end"]:
        raise ValueError("Long video requires gameplay scenes and a valid cold open")
    output.parent.mkdir(parents=True, exist_ok=True)
    intro_parts = cold_open.get("segments") or [cold_open]
    if any(right["source_start"] < left["source_end"] for left, right in zip(intro_parts, intro_parts[1:])):
        raise ValueError("Intro source ranges must be chronological and non-overlapping")
    source_parts = [*intro_parts, *segments]
    intro_seconds = intro_duration(cold_open)
    card_start = title_start(intro_seconds, title_duration, title_transition)
    if title_transition and (2 * title_transition >= title_duration or
                             segments[0]["source_end"] - segments[0]["source_start"] <= title_transition):
        raise ValueError("Title dissolves must fit inside the card and first gameplay scene")
    if title_transition and (end_duration <= title_transition or
                             segments[-1]["source_end"] - segments[-1]["source_start"] <= title_transition):
        raise ValueError("End dissolve must fit inside the end card and final gameplay scene")
    end_start = (card_start + title_duration - 2 * title_transition
                 + sum(segment["source_end"] - segment["source_start"] for segment in segments))
    if any(right["source_start"] < left["source_end"] for left, right in zip(segments, segments[1:])):
        raise ValueError("Long source segments must be chronological and non-overlapping")
    seeks = [_segment_input(source, part) for part in source_parts]
    chains: list[str] = []
    for index, (_arguments, left, right) in enumerate(seeks):
        chains.extend((
            f"[{index}:v]trim=start={left}:end={right},setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1,fps=30,settb=AVTB[v{index}]",
            f"[{index}:a]atrim=start={left}:end={right},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[a{index}]",
        ))
    title_input = len(source_parts)
    end_input = title_input + 1
    chains.extend((
        f"[{title_input}:v]trim=duration={title_duration},setpts=PTS-STARTPTS,setsar=1,fps=30,settb=AVTB[titlev]",
        f"[{title_input}:a]atrim=duration={title_duration - 2 * title_transition},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume=0[titlea]",
        f"[{end_input}:v]trim=duration={end_duration},setpts=PTS-STARTPTS,setsar=1,fps=30,settb=AVTB[endv]",
        f"[{end_input}:a]atrim=duration={end_duration - title_transition},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume=0[enda]",
    ))
    intro_v, intro_a = "v0", "a0"
    if len(intro_parts) > 1:
        chains.append(f"{''.join(f'[v{index}][a{index}]' for index in range(len(intro_parts)))}"
                      f"concat=n={len(intro_parts)}:v=1:a=1[preface][prefacea]")
        chains.append("[preface]fps=30,settb=AVTB[prefacev]")
        intro_v, intro_a = "prefacev", "prefacea"
    if title_transition:
        chains.extend((f"[{intro_v}][titlev]xfade=transition=fade:duration={title_transition}:offset={card_start}[introv]",
                       f"[{intro_a}][titlea]concat=n=2:v=0:a=1[introa]"))
        first = len(intro_parts)
        chains.extend((f"[introv][v{first}]xfade=transition=fade:duration={title_transition}:"
                       f"offset={card_start + title_duration - title_transition}[openingv]",
                       f"[introa][a{first}]concat=n=2:v=0:a=1[openinga]"))
        ordered = ["[openingv][openinga]"]
    else:
        ordered = [f"[{intro_v}][{intro_a}]", "[titlev][titlea]"]
    ordered += [f"[v{index}][a{index}]" for index in range(len(intro_parts) + bool(title_transition), len(source_parts))]
    if title_transition:
        chains.extend((f"{''.join(ordered)}concat=n={len(ordered)}:v=1:a=1[bodyv][bodya]",
                       "[bodyv]fps=30,settb=AVTB[bodyclock]",
                       f"[bodyclock][endv]xfade=transition=fade:duration={title_transition}:offset={end_start}[rawv]",
                       "[bodya][enda]concat=n=2:v=0:a=1[rawa]"))
    else:
        ordered.append("[endv][enda]")
        chains.append(f"{''.join(ordered)}concat=n={len(ordered)}:v=1:a=1[rawv][rawa]")
    chains.extend((
        "[rawv]fps=30,geq=lum='16+(lum(X,Y)-16)*(1-pow(1-T,3))':"
        "cb='128+(cb(X,Y)-128)*(1-pow(1-T,3))':"
        "cr='128+(cr(X,Y)-128)*(1-pow(1-T,3))':enable='lt(t,1)'[v]",
        "[rawa]loudnorm,aresample=48000[proga]",
    ))
    music_inputs: list[str] = []
    music_labels: list[str] = []
    next_input = end_input + 1
    if title_music is not None:
        window = title_music_window(intro_seconds, title_duration, dissolve=title_transition)
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
        delay = end_start - lead
        if delay < 0:
            raise ValueError("End music lead does not fit before the end card")
        music_inputs += ["-ss", str(end_music_start), "-t", str(length), "-i", str(end_music)]
        chains.append(
            f"[{next_input}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={end_music_volume},"
            f"afade=t=in:st=0:d={lead + 1.0}:curve=tri,afade=t=out:st={length - 1}:d=1:curve=qua,"
            f"adelay={round(delay * 1000)}:all=1[endmusic]"
        )
        music_labels.append("[endmusic]")
    if music_labels:
        chains.append(f"[proga]{''.join(music_labels)}amix=inputs={len(music_labels) + 1}:duration=first:dropout_transition=0:normalize=0[a]")
    else:
        chains.append("[proga]anull[a]")
    run_ffmpeg([
        *[argument for arguments, _left, _right in seeks for argument in arguments],
        "-t", str(title_duration), "-i", str(title_card),
        "-t", str(end_duration), "-i", str(end_card),
        *music_inputs,
        "-filter_complex", ";".join(chains),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
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
    seeks = [_segment_input(source, segment) for segment in segments]
    for index, (_arguments, left, right) in enumerate(seeks):
        chains.extend((f"[{index}:v]trim=start={left}:end={right},setpts=PTS-STARTPTS[v{index}]",
                       f"[{index}:a]atrim=start={left}:end={right},asetpts=PTS-STARTPTS,aresample=48000:first_pts=0[a{index}]"))
        inputs.append(f"[v{index}][a{index}]")
    chains.extend((f"{''.join(inputs)}concat=n={len(segments)}:v=1:a=1[rawv][rawa]",
                   "[rawv]split=3[face][game][hud]", f"[face]{face}[faceout]", f"[game]{game}[gameout]",
                    f"[hud]{score}[hudout]", "[gameout][hudout]overlay=x=W-w:y=0[gamehud]",
                   f"[faceout][gamehud]vstack=inputs=2,ass='{escaped_subtitles}':fontsdir='{fonts}',fps=30[v]",
                    "[rawa]loudnorm,aresample=48000[a]"))
    run_ffmpeg(
        [
            *[argument for arguments, _left, _right in seeks for argument in arguments],
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
