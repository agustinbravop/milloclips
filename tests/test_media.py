import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from milloclips.media import _kick_recording_url, sample_frames


class KickRecordingUrlTests(unittest.TestCase):
    def test_reads_embedded_hls_manifest(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{\\"recording_url\\":\\"https://stream.kick.com/vod/master.m3u8\\"}'

        with patch("milloclips.media.urlopen", return_value=Response()):
            self.assertEqual(
                _kick_recording_url("https://kick.com/elmiillor/videos/vod-id"),
                "https://stream.kick.com/vod/master.m3u8",
            )

    def test_ignores_non_kick_urls(self):
        self.assertIsNone(_kick_recording_url("https://example.com/video"))

    def test_resumes_cached_frames_when_end_is_known(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "frame_000000.jpg").touch()
            with patch("milloclips.media.run_ffmpeg") as run:
                sample_frames(Path("source.mp4"), output, 30, end=90)
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[arguments.index("-ss") + 1], "30")
            self.assertEqual(arguments[arguments.index("-start_number") + 1], "1")
