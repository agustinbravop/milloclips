from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from milloclips.kick import recent_vods


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
                    "category": "League of Legends",
                    "title": "League stream",
                    "url": "https://kick.com/elmiillor/videos/public-vod-id",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
