from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from milloclips.catalog import catalog_id, sync_catalog
from milloclips.io import read_json, write_json


EXISTING = "https://kick.com/elmiillor/videos/01a0cfac-7120-7744-86f8-45307de1111c"
NEW = "https://kick.com/elmiillor/videos/01a0f8d3-e740-76d7-847d-c00a0dfe40a4"


def _write_catalog(site: Path) -> Path:
    path = site / "static" / "catalog.json"
    write_json(path, {
        "generatedAt": "2026-09-30",
        "channel": {"name": "ElmilloR", "kickUrl": "https://kick.com/elmiillor"},
        "vods": [{"id": "809acd6b-dd4b-4660-b0ee-746f6398ad8e", "title": "ELISE BUFFS",
                  "date": "2026-09-23", "startTime": "2026-09-23T19:09:40Z", "duration": 14772,
                  "kickUrl": EXISTING, "long": {"id": "809acd6b-long"}, "shorts": []}],
    })
    return path


def _kick_payload() -> list[dict]:
    return [
        {"date": "2026-09-23T19:09:40Z", "duration_seconds": 14772, "duration": "04:06:12",
         "category": "League of Legends", "title": "ELISE BUFFS", "url": EXISTING},
        {"date": "2026-10-01T18:57:12Z", "duration_seconds": 12026, "duration": "03:20:26",
         "category": "League of Legends", "title": "1000LP STUCK", "url": NEW},
    ]


class CatalogSyncTests(unittest.TestCase):
    def test_adds_only_missing_streams_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            path = _write_catalog(site)
            with patch("milloclips.catalog.kick_vods", return_value=_kick_payload()):
                added = sync_catalog(site)
                self.assertEqual([vod["kickUrl"] for vod in added], [NEW])
                self.assertEqual(added[0]["id"], catalog_id(NEW))
                self.assertEqual(added[0]["pendingReason"], "not_ingested")
                self.assertEqual(added[0]["shorts"], [])
                self.assertEqual(added[0]["date"], "2026-10-01")

                catalog = read_json(path)
                self.assertEqual(len(catalog["vods"]), 2)
                self.assertEqual(sync_catalog(site), [])
                self.assertEqual(read_json(path), catalog)

    def test_never_mutates_the_existing_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            _write_catalog(site)
            with patch("milloclips.catalog.kick_vods", return_value=_kick_payload()):
                sync_catalog(site)
            vods = read_json(site / "static" / "catalog.json")["vods"]
            kept = next(vod for vod in vods if vod["kickUrl"] == EXISTING)
            self.assertEqual(kept, {"id": "809acd6b-dd4b-4660-b0ee-746f6398ad8e", "title": "ELISE BUFFS",
                                    "date": "2026-09-23", "startTime": "2026-09-23T19:09:40Z", "duration": 14772,
                                    "kickUrl": EXISTING, "long": {"id": "809acd6b-long"}, "shorts": []})

    def test_passes_the_date_window_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            _write_catalog(site)
            with patch("milloclips.catalog.kick_vods", return_value=[]) as fetch:
                sync_catalog(site, channel="elmiillor", since="2026-09-17", until="2026-10-01")
            self.assertEqual(fetch.call_args.args, ("elmiillor",))
            self.assertEqual(fetch.call_args.kwargs, {"league_only": True, "since": "2026-09-17", "until": "2026-10-01"})

    def test_added_entry_satisfies_the_website_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            _write_catalog(site)
            with patch("milloclips.catalog.kick_vods", return_value=_kick_payload()):
                sync_catalog(site)
            added = next(vod for vod in read_json(site / "static" / "catalog.json")["vods"] if vod["kickUrl"] == NEW)
            for field in ("id", "title", "date", "startTime", "duration", "kickUrl", "shorts"):
                self.assertIn(field, added)
            self.assertIsInstance(added["duration"], int)
            self.assertIsInstance(added["shorts"], list)


if __name__ == "__main__":
    unittest.main()