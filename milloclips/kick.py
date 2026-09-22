from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from .io import format_time


def recent_vods(channel: str, limit: int, league_only: bool = False) -> list[dict]:
    url = f"https://kick.com/api/v2/channels/{quote(channel, safe='')}/videos"
    request = Request(url, headers={"User-Agent": "milloclips/0.1"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise RuntimeError("Kick returned an unexpected VOD response")

    vods = []
    for item in payload:
        categories = item.get("categories") or []
        category = categories[0] if categories else {}
        if league_only and category.get("slug") != "league-of-legends":
            continue
        uuid = (item.get("video") or {}).get("uuid")
        if not uuid:
            continue
        vods.append(
            {
                "date": item.get("start_time") or item.get("created_at") or "unknown",
                "duration": format_time((item.get("duration") or 0) / 1000),
                "category": category.get("name") or "Unknown",
                "title": " ".join((item.get("session_title") or "Untitled").split()),
                "url": f"https://kick.com/{channel}/videos/{uuid}",
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
