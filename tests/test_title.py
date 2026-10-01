import unittest
import math
import struct
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageStat

from milloclips.editing import _audio_spectrum, _title_frame


class TitleTests(unittest.TestCase):
    def test_exit_reverses_the_one_second_entrance_at_the_requested_duration(self):
        logo = Image.new("RGBA", (64, 64), "white")
        for duration in (5, 7):
            for time in (0, 0.2, 0.5, 0.8, 1):
                entrance = _title_frame(logo, time, [1.0] * 48, duration)
                exit_frame = _title_frame(logo, duration - time, [1.0] * 48, duration)
                self.assertLessEqual(max(abs(a - b) for a, b in zip(
                    entrance.getpixel((640, 360)), exit_frame.getpixel((640, 360)))), 3)

    def test_icon_is_centered_and_fades_in_without_text_or_flash(self):
        logo = Image.new("RGBA", (64, 64), "white")
        with patch.object(ImageDraw.ImageDraw, "text", side_effect=AssertionError("Title must not contain text")):
            frames = [_title_frame(logo, time, [1.0] * 48) for time in (0, 0.2, 0.5, 1)]
        brightness = [sum(ImageStat.Stat(frame.crop((500, 220, 780, 500))).mean) for frame in frames]
        self.assertEqual(brightness, sorted(brightness))
        self.assertLess(brightness[1], brightness[-1] / 3)
        self.assertEqual(frames[-1].getpixel((640, 360)), (255, 255, 255))
        self.assertLess(frames[-1].getpixel((640, 590))[0], 250)
        # With constant spectrum the ring has equal reach above and below the centered icon.
        top, bottom = frames[-1].getpixel((640, 52)), frames[-1].getpixel((640, 668))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(top, bottom)), 2)

    def test_ring_has_large_audio_driven_excursion_and_icon_breathes(self):
        logo = Image.new("RGBA", (64, 64), "white")
        quiet = _title_frame(logo, 1, [0.0] * 48)
        loud = _title_frame(logo, 1, [1.0] * 48)
        self.assertGreater(loud.getpixel((640, 52))[0], quiet.getpixel((640, 52))[0] + 100)
        self.assertLess(quiet.getpixel((785, 360))[0], 255)
        expanded = _title_frame(logo, 2.05, [0.0] * 48)
        self.assertEqual(expanded.getpixel((785, 360)), (255, 255, 255))

    def test_spectrum_responds_on_the_exact_audio_frame_without_a_tail(self):
        def decode(arguments):
            samples = [round(20000 * math.sin(2 * math.pi * 300 * index / 3000))
                       if 1500 <= index < 1600 else 0 for index in range(3000)]
            with wave.open(arguments[-1], "wb") as audio:
                audio.setparams((1, 2, 3000, 0, "NONE", "not compressed"))
                audio.writeframes(struct.pack("<3000h", *samples))

        with patch("milloclips.editing.run_ffmpeg", side_effect=decode) as ffmpeg:
            spectra = _audio_spectrum(Path("song.webm"), 43.5, 1)
        self.assertEqual(ffmpeg.call_args.args[0][:4], ["-ss", "43.5", "-t", "1"])
        self.assertGreater(max(spectra[15]), 0.9)
        self.assertTrue(all(max(frame) == 0 for index, frame in enumerate(spectra) if index != 15))
