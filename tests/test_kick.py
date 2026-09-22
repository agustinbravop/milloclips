from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from milloclips.kick import recent_vods


class KickTests(unittest.TestCase):
    @patch("milloclips.kick.urlopen")
    def test_normalizes_and_filters_vods(self, urlopen) -> None:
        payload = [
            {
                "start_time": "2026-09-20 19:37:38",
                "duration": 27_785_000,
                "session_title": "  League   stream ",
                "categories": [{"name": "League of Legends", "slug": "league-of-legends"}],
                "video": {"uuid": "vod-uuid"},
            },
            {
                "duration": 1000,
                "categories": [{"name": "Slots & Casino", "slug": "slots"}],
                "video": {"uuid": "ignored"},
            },
        ]
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())

        self.assertEqual(
            recent_vods("elmiillor", 10, league_only=True),
            [
                {
                    "date": "2026-09-20 19:37:38",
                    "duration": "07:43:05",
                    "category": "League of Legends",
                    "title": "League stream",
                    "url": "https://kick.com/elmiillor/videos/vod-uuid",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
