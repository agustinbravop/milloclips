import unittest
import math
import struct
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from milloclips.media import _kick_recording_url, media_info, render_long_video, run_ffmpeg, sample_boundary_frames, sample_frames


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


class BoundaryMediaTests(unittest.TestCase):
    def test_card_dissolves_blend_frames_and_account_for_each_overlap(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, card, output = root / "red.mp4", root / "blue.mp4", root / "dissolve.mp4"
            for path, color in ((source, "red"), (card, "blue")):
                run_ffmpeg(["-f", "lavfi", "-i", f"color={color}:s=1280x720:r=30:d=6", "-f", "lavfi", "-i",
                            "anullsrc=r=48000:cl=stereo", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "aac", str(path)], capture=True)
            with patch("milloclips.media.run_ffmpeg", side_effect=lambda args: run_ffmpeg(args, capture=True)):
                render_long_video(source, [{"source_start": 3, "source_end": 4}],
                                  {"source_start": 0, "source_end": 3, "segments": [
                                      {"source_start": 0, "source_end": 1}, {"source_start": 2, "source_end": 3}]}, card, card, output,
                                   title_duration=2, end_duration=1)
            self.assertAlmostEqual(media_info(output)["duration"], 4.5, delta=0.1)
            opening = sample_boundary_frames(output, root / "opening", 0.5, start=0, end=1.1, width=64)
            reds = [Image.open(frame["path"]).getpixel((32, 18))[0] for frame in opening]
            self.assertLess(reds[0], 8)  # Allow JPEG/codec rounding near black.
            self.assertAlmostEqual(reds[1] / reds[2], 0.875, delta=0.04)
            frames = sample_boundary_frames(output, root / "frames", 0.25, start=1.5, end=2.1, width=64)
            colors = [Image.open(frame["path"]).getpixel((32, 18)) for frame in frames]
            self.assertGreater(colors[0][0], 200)
            self.assertGreater(colors[1][0], 60)
            self.assertGreater(colors[1][2], 60)
            self.assertGreater(colors[-1][2], 200)
            exit_frames = sample_boundary_frames(output, root / "exit", 0.25, start=3, end=3.6, width=64)
            exit_colors = [Image.open(frame["path"]).getpixel((32, 18)) for frame in exit_frames]
            self.assertGreater(exit_colors[0][2], 200)
            self.assertGreater(exit_colors[1][0], 60)
            self.assertGreater(exit_colors[1][2], 60)
            self.assertGreater(exit_colors[-1][0], 200)
            outro = sample_boundary_frames(output, root / "outro", 0.25, start=3.5, end=4.1, width=64)
            outro_colors = [Image.open(frame["path"]).getpixel((32, 18)) for frame in outro]
            self.assertGreater(outro_colors[0][0], 200)
            self.assertGreater(outro_colors[1][0], 60)
            self.assertGreater(outro_colors[1][2], 60)
            self.assertGreater(outro_colors[-1][2], 200)

    def test_decoded_timestamps_and_rendered_cuts_with_nonzero_source_offset(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "offset.mp4"
            run_ffmpeg([
                "-f", "lavfi", "-i", "color=red:s=64x64:r=60:d=4,drawbox=color=lime:t=fill:enable='gte(t,2)'",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4,volume=0:enable='lt(t,2)'",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-output_ts_offset", "62", str(source),
            ], capture=True)
            frames = sample_boundary_frames(source, root / "sampled", 0.25, start=1.37, end=3, width=64)
            self.assertGreaterEqual(frames[0]["timestamp"], 1.37)
            self.assertLess(frames[0]["timestamp"], 1.37 + 1 / 60 + 0.025)
            self.assertTrue(all(a["timestamp"] < b["timestamp"] for a, b in zip(frames, frames[1:])))
            manifest = root / "sampled" / "frames.json"
            self.assertTrue(manifest.exists())
            with patch("milloclips.media.run_ffmpeg") as cached:
                self.assertEqual(sample_boundary_frames(source, root / "sampled", 0.25, start=1.37, end=3, width=64), frames)
                cached.assert_not_called()

            card = root / "card.mp4"
            run_ffmpeg(["-f", "lavfi", "-i", "color=gray:s=1280x720:r=30:d=0.1", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-t", "0.1", "-c:v", "libx264", "-c:a", "aac", str(card)], capture=True)
            output = root / "cut.mp4"
            # Use the real long-form renderer, including cards and non-frame-aligned source seeks.
            with patch("milloclips.media.run_ffmpeg", side_effect=lambda args: run_ffmpeg(args, capture=True)):
                render_long_video(source, [{"source_start": 1.73, "source_end": 2.48}],
                                  {"source_start": 0.25, "source_end": 0.45}, card, card, output,
                                  title_duration=0.1, end_duration=0.1, title_transition=0)
            self.assertAlmostEqual(media_info(output)["duration"], 1.15, delta=0.1)
            rendered = sample_boundary_frames(output, root / "rendered", 1 / 30, start=0.3, end=1.05, width=64)
            green = next(frame["timestamp"] for frame in rendered
                         if Image.open(frame["path"]).getpixel((32, 32))[1] > 150)
            self.assertAlmostEqual(green, 0.3 + 2 - 1.73, delta=2 / 30)
            audio = root / "audio.wav"
            run_ffmpeg(["-i", str(output), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(audio)], capture=True)
            with wave.open(str(audio), "rb") as sound:
                samples = struct.unpack(f"<{sound.getnframes()}h", sound.readframes(sound.getnframes()))
            energies = [math.sqrt(sum(x * x for x in samples[i:i + 480]) / len(samples[i:i + 480]))
                        for i in range(0, len(samples), 480)]
            audible = next(i * 0.01 for i, energy in enumerate(energies) if energy > max(energies) * 0.3)
            self.assertAlmostEqual(audible, green, delta=2 / 30)
