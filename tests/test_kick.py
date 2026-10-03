from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from milloclips.kick import kick_vods, recent_vods


class KickTests(unittest.TestCase):
    @patch("milloclips.kick.urlopen")
    def test_normalizes_and_filters_vods(self, urlopen) -> None:
        responses = [
            {"id": 6100370},
            {
                "data": [
                    {
                        "id": "public-vod-id",
                        "start_time": "2026-09-20T19:37:38Z",
                        "duration": 27_785,
                        "title": "  League   stream  | !verificar !tienda ",
                        "category": {"name": "League of Legends", "slug": "league-of-legends"},
                    },
                    {
                        "id": "ignored",
                        "duration": 1,
                        "category": {"name": "Slots & Casino", "slug": "slots"},
                    },
                ]
            },
        ]
        urlopen.return_value.__enter__.side_effect = [
            io.BytesIO(json.dumps(response).encode()) for response in responses
        ]

        self.assertEqual(
            recent_vods("elmiillor", 10, league_only=True),
            [
                {
                    "date": "2026-09-20T19:37:38Z",
                    "duration": "07:43:05",
                    "duration_seconds": 27_785,
                    "category": "League of Legends",
                    "title": "League stream",
                    "url": "https://kick.com/elmiillor/videos/public-vod-id",
                }
            ],
        )

    @patch("milloclips.kick.urlopen")
    def test_filters_by_date_window_inclusively(self, urlopen) -> None:
        urlopen.return_value.__enter__.side_effect = [
            io.BytesIO(json.dumps({"id": 1}).encode()),
            io.BytesIO(json.dumps({"data": [
                {"id": "early", "start_time": "2026-09-16T18:00:00Z", "duration": 10,
                 "title": "a", "category": {"slug": "league-of-legends"}},
                {"id": "edge-start", "start_time": "2026-09-17T00:00:00Z", "duration": 10,
                 "title": "b", "category": {"slug": "league-of-legends"}},
                {"id": "edge-end", "start_time": "2026-10-01T23:00:00Z", "duration": 10,
                 "title": "c", "category": {"slug": "league-of-legends"}},
                {"id": "late", "start_time": "2026-10-02T00:00:00Z", "duration": 10,
                 "title": "d", "category": {"slug": "league-of-legends"}},
            ]}).encode()),
        ]

        self.assertEqual(
            [vod["url"] for vod in kick_vods("elmiillor", since="2026-09-17", until="2026-10-01")],
            ["https://kick.com/elmiillor/videos/edge-start", "https://kick.com/elmiillor/videos/edge-end"],
        )

    def test_rejects_malformed_or_inverted_window(self) -> None:
        for since, until in [("2026-9-17", None), ("not-a-date", None), ("2026-10-01", "2026-09-17")]:
            with self.subTest(since=since, until=until), self.assertRaises(ValueError):
                kick_vods("elmiillor", since=since, until=until)


if __name__ == "__main__":
    unittest.main()
