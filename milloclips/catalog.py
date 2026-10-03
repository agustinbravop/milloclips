"""Upsert Kick streams into the website catalog so publish can match them."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from .io import read_json, write_json
from .kick import kick_vods

CATALOG_NAMESPACE = uuid.UUID("6f1d0b4a-6c3f-5a2e-9b7c-2f5d8e14a903")


def catalog_id(kick_url: str) -> str:
    """Stable id from the Kick URL; publish keys off kickUrl, this is just identity."""
    return str(uuid.uuid5(CATALOG_NAMESPACE, kick_url))


def sync_catalog(site: Path, channel: str = "elmiillor", since: str | None = None,
                 until: str | None = None) -> list[dict]:
    """Add catalog entries for Kick streams that have none. Never edits existing ones."""
    path = site / "static" / "catalog.json"
    catalog = read_json(path)
    known = {vod.get("kickUrl") for vod in catalog["vods"]}
    added = []
    for vod in kick_vods(channel, league_only=True, since=since, until=until):
        if vod["url"] in known:
            continue
        added.append({
            "id": catalog_id(vod["url"]),
            "title": vod["title"],
            "date": vod["date"][:10],
            "startTime": vod["date"],
            "duration": vod["duration_seconds"],
            "kickUrl": vod["url"],
            "shorts": [],
            "pendingReason": "not_ingested",
        })
    if added:
        catalog["vods"].extend(added)
        catalog["vods"].sort(key=lambda vod: vod.get("startTime", ""), reverse=True)
        catalog["generatedAt"] = datetime.now(UTC).strftime("%Y-%m-%d")
        write_json(path, catalog)
    return added