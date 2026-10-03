from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from .io import format_time


def _load_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "milloclips/0.1"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _in_window(start_time: str, since: str | None, until: str | None) -> bool:
    # Kick start_time is ISO-8601 UTC, so the date prefix compares lexicographically.
    day = start_time[:10]
    if len(day) != 10 or not start_time:
        return False
    return (since is None or day >= since) and (until is None or day <= until)


def kick_vods(channel: str, league_only: bool = False, since: str | None = None,
              until: str | None = None) -> list[dict]:
    """Every VOD the channel endpoint returns, normalized and window-filtered."""
    for value in (since, until):
        if value is not None and (len(value) != 10 or not _is_date(value)):
            raise ValueError(f"Dates must be YYYY-MM-DD: {value}")
    if since and until and since > until:
        raise ValueError("--since must not be after --until")

    channel_data = _load_json(f"https://kick.com/api/v2/channels/{quote(channel, safe='')}")
    channel_id = channel_data.get("id") if isinstance(channel_data, dict) else None
    if not channel_id:
        raise RuntimeError("Kick returned an unexpected channel response")

    payload = _load_json(f"https://web.kick.com/api/v1/channels/{channel_id}/videos")
    videos = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        raise RuntimeError("Kick returned an unexpected VOD response")

    vods = []
    for item in videos:
        category = item.get("category") or {}
        if league_only and category.get("slug") != "league-of-legends":
            continue
        vod_id = item.get("id")
        start_time = item.get("start_time") or ""
        if not vod_id or not _in_window(start_time, since, until):
            continue
        title = " ".join((item.get("title") or "Untitled").split()).split(" | !", 1)[0]
        vods.append(
            {
                "date": start_time,
                "duration": format_time(item.get("duration") or 0),
                "duration_seconds": item.get("duration") or 0,
                "category": category.get("name") or "Unknown",
                "title": title,
                "url": f"https://kick.com/{channel}/videos/{vod_id}",
            }
        )
    return vods


def _is_date(value: str) -> bool:
    return value[:4].isdigit() and value[4] == "-" and value[5:7].isdigit() and value[7] == "-" and value[8:10].isdigit()


def recent_vods(channel: str, limit: int, league_only: bool = False) -> list[dict]:
    return kick_vods(channel, league_only)[:limit]


def print_vods(vods: list[dict]) -> None:
    for vod in vods:
        print(f"{vod['date']}  {vod['duration']}  {vod['category']}")
        print(vod["title"])
        print(vod["url"])
        print()