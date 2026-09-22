from __future__ import annotations

from pathlib import Path

from .media import ffmpeg, ingest


def create_fixture(root: Path, seconds: int = 1200) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "synthetic-vod.mp4"
    if not source.exists():
        import subprocess

        subprocess.run(
            [
                ffmpeg(),
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=320x180:rate=5:duration={seconds}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=16000:duration={seconds}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "32",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ],
            check=True,
        )
    return ingest(str(source), root / "workspace")
