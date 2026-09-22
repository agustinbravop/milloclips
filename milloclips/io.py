from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def require(path: Path, message: str | None = None) -> Path:
    if not path.exists():
        raise SystemExit(message or f"Missing required artifact: {path}")
    return path


def format_time(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def record_run(vod_dir: Path, stage: str, *, model: str | None = None, mock: bool = False) -> None:
    path = vod_dir / "run.json"
    value = read_json(path) if path.exists() else {"events": []}
    value["events"].append(
        {"stage": stage, "model": model, "mock": mock, "finished_at": datetime.now(UTC).isoformat()}
    )
    write_json(path, value)
