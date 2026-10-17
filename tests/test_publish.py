import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from milloclips.io import read_json, write_json
from milloclips.media import run_ffmpeg
from milloclips.publish import kick_path_id, match_vod, publish, publish_confirmed, slugify, thumbnail_selection
from unittest.mock import patch


KICK_URL = "https://kick.com/elmiillor/videos/01a0eece-e6a0-78f3-aaf8-c5743a1d6443"


def _write_catalog(site: Path) -> Path:
    catalog = {
        "generatedAt": "2026-09-30",
        "channel": {"name": "ElmilloR", "kickUrl": "https://kick.com/elmiillor"},
        "vods": [
            {
                "id": "10b65baf-dd99-4139-82ea-804f49ae37c0",
                "title": "Chill gaming",
                "date": "2026-09-29",
                "startTime": "2026-09-29T20:15:32Z",
                "duration": 14368,
                "kickUrl": KICK_URL,
                "shorts": [],
                "pendingReason": "not_ingested",
            }
        ],
    }
    path = site / "static" / "catalog.json"
    write_json(path, catalog)
    return path


def _write_workspace(vod_dir: Path) -> None:
    write_json(vod_dir / "manifest.json", {"vod_id": "7ecad9c2b4a0", "duration": 14367.82,
                                           "metadata": {"kind": "kick", "input": KICK_URL}})
    write_json(vod_dir / "long-plan.json", {"duration": 645.7900077343002})
    (vod_dir / "renders" / "thumbnails").mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(vod_dir / "renders" / "thumbnail.png")
    write_json(vod_dir / "renders" / "thumbnails" / "variants.json",
               [{"title": "Udyr sin freno"}, {"title": "Other"}, {"title": "Another"}])
    write_json(vod_dir / "renders" / "thumbnails" / "selection.json",
               {"status": "pending", "provisional": 1})


class PublishHelpersTests(unittest.TestCase):
    def test_slugify_is_ascii_and_deterministic(self):
        self.assertEqual(slugify("BARÓN EN EL ABISMO"), "baron-en-el-abismo")
        self.assertEqual(slugify("  "), "stream")

    def test_kick_path_id_extracts_video_id(self):
        self.assertEqual(kick_path_id(KICK_URL), "01a0eece-e6a0-78f3-aaf8-c5743a1d6443")
        self.assertIsNone(kick_path_id("https://example.com/video"))

    def test_match_vod_by_kick_url(self):
        catalog = {"vods": [{"id": "x", "kickUrl": KICK_URL}]}
        self.assertEqual(match_vod(catalog, kick_path_id(KICK_URL), None)["id"], "x")
        self.assertIsNone(match_vod(catalog, None, "missing"))

    def test_thumbnail_selection_falls_back_to_provisional(self):
        with TemporaryDirectory() as directory:
            vod_dir = Path(directory)
            _write_workspace(vod_dir)
            title, poster = thumbnail_selection(vod_dir)
            self.assertEqual(title, "Udyr sin freno")
            self.assertTrue(poster.exists())


class PublishTests(unittest.TestCase):
    def test_confirmed_individual_upload_preserves_siblings_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site, vod_dir = root / "website", root / "vod"
            vod_dir.mkdir()
            catalog_path = _write_catalog(site)
            _write_workspace(vod_dir)
            catalog = read_json(catalog_path)
            vod = catalog["vods"][0]
            vod["long"] = {"id": "existing-long", "video": {"provider": "youtube", "src": "long-id"}}
            sibling = {"id": f"{vod['id']}-short-02", "video": {"provider": "youtube", "src": "sibling-id"}}
            vod["shorts"] = [sibling]
            write_json(catalog_path, catalog)
            state = {"confirmed": True, "privacy": "unlisted", "video_id": "short-id", "artifact": {
                "clip": "short-01", "path": "unused.mp4", "title": "Generated short", "duration": 30,
                "width": 1080, "height": 1920}}
            def poster(video, destination):
                destination.write_bytes(b"poster")
            with patch("milloclips.publish._poster", side_effect=poster):
                publish_confirmed(vod_dir, site, state)
                state["artifact"]["clip"] = "short-v8-01"
                publish_confirmed(vod_dir, site, state)
            updated = read_json(catalog_path)["vods"][0]
            self.assertEqual(updated["long"], vod["long"])
            self.assertEqual(updated["shorts"][0], sibling)
            self.assertEqual(len(updated["shorts"]), 2)
            self.assertEqual(updated["shorts"][1]["video"], {"provider": "youtube", "src": "short-id"})
            self.assertNotIn("pendingReason", updated)

    def test_confirmed_long_uses_uploaded_thumbnail_and_preserves_shorts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site, vod_dir = root / "website", root / "vod"
            vod_dir.mkdir()
            catalog_path = _write_catalog(site)
            _write_workspace(vod_dir)
            catalog = read_json(catalog_path)
            catalog["vods"][0]["shorts"] = [{"id": "existing-short"}]
            write_json(catalog_path, catalog)
            (vod_dir / "youtube").mkdir()
            Image.new("RGB", (1280, 720)).save(vod_dir / "youtube" / "long-thumbnail.jpg")
            state = {"confirmed": True, "privacy": "unlisted", "video_id": "long-id", "artifact": {
                "clip": "long", "title": "Generated title", "duration": 600, "width": 1280, "height": 720}}
            publish_confirmed(vod_dir, site, state)
            updated = read_json(catalog_path)["vods"][0]
            self.assertEqual(updated["long"]["title"], "Generated title")
            self.assertEqual(updated["long"]["video"], {"provider": "youtube", "src": "long-id"})
            self.assertEqual(updated["shorts"], [{"id": "existing-short"}])

    def test_gallery_rejects_unconfirmed_upload(self):
        with self.assertRaisesRegex(SystemExit, "confirmed Unlisted"):
            publish_confirmed(Path("unused"), Path("unused"), {"privacy": "private"})

    def test_requires_approval(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                publish(Path(directory), Path(directory), approve=False)

    def test_publishes_youtube_long_and_clears_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "website"
            vod_dir = root / "workspace" / "7ecad9c2b4a0"
            vod_dir.mkdir(parents=True)
            catalog_path = _write_catalog(site)
            _write_workspace(vod_dir)

            result = publish(vod_dir, site, approve=True, youtube="abc123")

            self.assertEqual(result, catalog_path)
            vod = read_json(catalog_path)["vods"][0]
            self.assertNotIn("pendingReason", vod)
            self.assertEqual(vod["long"]["video"], {"provider": "youtube", "src": "abc123"})
            self.assertEqual(vod["long"]["title"], "Udyr sin freno")
            self.assertEqual(vod["long"]["duration"], 645.79)
            self.assertEqual(vod["long"]["poster"], "/images/udyr-sin-freno-thumbnail.png")
            self.assertTrue((site / "static" / "images" / "udyr-sin-freno-thumbnail.png").exists())
            self.assertEqual(vod["shorts"], [])

    def test_publishes_file_long_into_static(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "website"
            vod_dir = root / "workspace" / "7ecad9c2b4a0"
            vod_dir.mkdir(parents=True)
            catalog_path = _write_catalog(site)
            _write_workspace(vod_dir)
            render = vod_dir / "renders" / "long-rough.mp4"
            run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=1280x720:r=30:d=0.2",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(render)])

            publish(vod_dir, site, approve=True)

            vod = read_json(catalog_path)["vods"][0]
            self.assertEqual(vod["long"]["video"],
                             {"provider": "file", "src": "/media/udyr-sin-freno-long.mp4"})
            self.assertEqual(vod["long"]["duration"], 0.2)
            self.assertTrue((site / "static" / "media" / "udyr-sin-freno-long.mp4").exists())

    def test_publishes_without_generated_thumbnail_using_video_frame(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site, vod_dir = root / "website", root / "workspace" / "7ecad9c2b4a0"
            vod_dir.mkdir(parents=True)
            catalog_path = _write_catalog(site)
            _write_workspace(vod_dir)
            (vod_dir / "renders" / "thumbnail.png").unlink()
            render = vod_dir / "renders" / "long-rough.mp4"
            run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=1280x720:r=30:d=0.2",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(render)])

            with patch("milloclips.publish._poster", side_effect=lambda video, image: Image.new("RGB", (8, 8)).save(image)):
                publish(vod_dir, site, approve=True)

            vod = read_json(catalog_path)["vods"][0]
            self.assertEqual(vod["long"]["title"], "Chill gaming")
            self.assertEqual(vod["long"]["poster"], "/images/chill-gaming-long.jpg")
            self.assertTrue((site / "static" / "images" / "chill-gaming-long.jpg").exists())


if __name__ == "__main__":
    unittest.main()
