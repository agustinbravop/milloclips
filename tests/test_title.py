import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageStat

from milloclips.editing import _title_frame


class TitleTests(unittest.TestCase):
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
        top, bottom = frames[-1].getpixel((640, 128)), frames[-1].getpixel((640, 592))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(top, bottom)), 2)
