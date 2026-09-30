from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from .io import format_time


def _load_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "milloclips/0.1"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def recent_vods(channel: str, limit: int, league_only: bool = False) -> list[dict]:
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
        if not vod_id:
            continue
        title = " ".join((item.get("title") or "Untitled").split()).split(" | !", 1)[0]
        vods.append(
            {
                "date": item.get("start_time") or "unknown",
                "duration": format_time(item.get("duration") or 0),
                "category": category.get("name") or "Unknown",
                "title": title,
                "url": f"https://kick.com/{channel}/videos/{vod_id}",
            }
        )
        if len(vods) == limit:
            break
    return vods


def print_vods(vods: list[dict]) -> None:
    for vod in vods:
        print(f"{vod['date']}  {vod['duration']}  {vod['category']}")
        print(vod["title"])
        print(vod["url"])
        print()
