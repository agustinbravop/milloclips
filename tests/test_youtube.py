import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from requests import ConnectionError

from milloclips.io import read_json, write_json
from milloclips.youtube import _channel, _resume_video, confirm_video, upload_video, video_artifact


def response(status, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = body or {}
    return result


class YouTubeTests(unittest.TestCase):
    def test_resume_after_interrupted_chunk_and_recover_lost_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"12345678")
            state_path = root / "youtube" / "long.json"
            state = {"artifact": {"path": str(video), "size": 8, "title": "Title", "description": "Source"}}
            session = Mock()
            session.post.return_value = response(200, headers={"Location": "https://upload.example/session"})
            session.put.side_effect = [response(308), response(308, headers={"Range": "bytes=0-3"}), ConnectionError()]
            with patch("milloclips.youtube.CHUNK_SIZE", 4):
                with self.assertRaises(ConnectionError):
                    _resume_video(session, state, state_path)
            self.assertEqual(session.post.call_args.kwargs["json"]["status"]["privacyStatus"], "unlisted")
            self.assertEqual(session.post.call_args.kwargs["params"]["notifySubscribers"], "false")
            self.assertEqual(session.put.call_args.kwargs["headers"]["Content-Range"], "bytes 4-7/8")
            saved = read_json(state_path)
            self.assertIn("session_url", saved)
            self.assertNotIn("video_id", saved)

            # YouTube accepted the final chunk but the client lost its response.
            resumed = Mock()
            resumed.put.return_value = response(200, {"id": "uploaded", "status": {"privacyStatus": "private"}})
            _resume_video(resumed, saved, state_path)
            resumed.post.assert_not_called()
            self.assertEqual(read_json(state_path)["video_id"], "uploaded")
            self.assertNotIn("session_url", read_json(state_path))
            self.assertEqual(resumed.put.call_args.kwargs["headers"]["Content-Range"], "bytes */8")

    def test_resume_sends_only_remaining_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"12345678")
            state = {"artifact": {"path": str(video), "size": 8}, "session_url": "https://upload.example/session"}
            session = Mock()
            session.put.side_effect = [response(308, headers={"Range": "bytes=0-3"}), response(200, {"id": "done"})]
            _resume_video(session, state, root / "state.json")
            session.post.assert_not_called()
            self.assertEqual(session.put.call_args.kwargs["data"], b"5678")

    def test_expired_session_never_starts_replacement(self):
        with TemporaryDirectory() as directory:
            state = {"artifact": {"size": 8}, "session_url": "https://upload.example/session"}
            session = Mock()
            session.put.return_value = response(404)
            with self.assertRaisesRegex(SystemExit, "expired"):
                _resume_video(session, state, Path(directory) / "state.json")
            session.post.assert_not_called()

    def test_daily_limit_keeps_state_and_reports_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {"clip": "short-01", "path": "unused", "size": 8, "title": "Title", "description": "Source"}
            session = Mock()
            session.post.return_value = response(400, {"error": {"errors": [{"reason": "uploadLimitExceeded"}]}})
            with patch("milloclips.youtube.video_artifact", return_value=artifact), \
                 patch("milloclips.youtube.connect", return_value=(session, {"id": "channel"})):
                with self.assertRaisesRegex(SystemExit, "daily channel upload limit"):
                    upload_video(root, "short-01", root, True)
            state = read_json(root / "youtube" / "short-01.json")
            self.assertEqual(state["artifact"], artifact)
            self.assertNotIn("video_id", state)

    def test_thumbnail_failure_retries_without_uploading_video_again(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "renders").mkdir()
            Image.new("RGB", (1280, 720)).save(root / "renders" / "thumbnail.png")
            artifact = {"clip": "long", "path": "unused", "size": 8, "title": "Title", "description": "Source"}
            session = Mock()
            session.post.side_effect = [response(403, {"error": {"errors": [{"reason": "forbidden"}]}}), response(200)]

            def uploaded(session, state, path):
                state.update(video_id="saved", url="https://www.youtube.com/watch?v=saved")
                write_json(path, state)

            with patch("milloclips.youtube.video_artifact", return_value=artifact), \
                 patch("milloclips.youtube.connect", return_value=(session, {"id": "channel"})), \
                 patch("milloclips.youtube._resume_video", side_effect=uploaded) as resume:
                with self.assertRaisesRegex(SystemExit, "forbidden"):
                    upload_video(root, "long", root, True)
                self.assertEqual(read_json(root / "youtube" / "long.json")["video_id"], "saved")
                state = upload_video(root, "long", root, True)
                self.assertTrue(state["thumbnail_uploaded"])
                resume.assert_called_once()
                upload_video(root, "long", root, True)
                self.assertEqual(session.post.call_count, 2)

    def test_changed_artifacts_stop_before_network_or_duplicate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "youtube" / "long.json", {"artifact": {"sha256": "original"}, "video_id": "saved"})
            with patch("milloclips.youtube.video_artifact", return_value={"sha256": "changed"}), \
                 patch("milloclips.youtube.connect") as connect:
                with self.assertRaisesRegex(SystemExit, "artifacts changed"):
                    upload_video(root, "long", root, True)
                connect.assert_not_called()

    def test_confirmation_requires_unlisted_processed_and_embedding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {"clip": "short-01"}
            state_path = root / "youtube" / "short-01.json"
            write_json(state_path, {"artifact": artifact, "video_id": "saved", "channel_id": "channel"})
            session = Mock()
            video = {"snippet": {"channelId": "channel"},
                     "status": {"privacyStatus": "unlisted", "uploadStatus": "processed", "embeddable": True},
                     "processingDetails": {"processingStatus": "succeeded"}}
            with patch("milloclips.youtube.video_artifact", return_value=artifact), \
                 patch("milloclips.youtube.connect", return_value=(session, {"id": "channel"})):
                for key, value, message in [("privacyStatus", "private", "not Unlisted"),
                                            ("uploadStatus", "uploaded", "not successfully processed"),
                                            ("embeddable", False, "Enable embedding")]:
                    original = video["status"][key]
                    video["status"][key] = value
                    session.get.return_value = response(200, {"items": [video]})
                    with self.assertRaisesRegex(SystemExit, message):
                        confirm_video(root, "short-01", root, True)
                    self.assertNotIn("confirmed", read_json(state_path))
                    video["status"][key] = original
                session.get.return_value = response(200, {"items": [video]})
                self.assertTrue(confirm_video(root, "short-01", root, True)["confirmed"])

    def test_wrong_channel_rejected(self):
        session = Mock()
        session.get.return_value = response(200, {"items": [{"id": "wrong", "snippet": {"customUrl": "@wrong", "title": "Wrong"}}]})
        with self.assertRaisesRegex(SystemExit, "Wrong YouTube channel"):
            _channel(session, "@agustinbravop")

    def test_artifact_checks_short_duration_and_title(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "short-01.mp4"
            video.write_bytes(b"video")
            item = {"render_name": "short-01", "render": video, "candidate": {"title": "Una jugada"}}
            write_json(root / "manifest.json", {"metadata": {"input": "https://kick.com/elmiillor/videos/source"}})
            info = {"width": 1080, "height": 1920, "audio": True, "duration": 45}
            with patch("milloclips.youtube.validated_short_renders", return_value=[item]), \
                 patch("milloclips.youtube.media_info", return_value=info):
                artifact = video_artifact(root, "short-01")
                self.assertEqual(artifact["title"], "Una jugada")
                self.assertIn("kick.com", artifact["description"])
                item["render_name"] = "short-v8-01"
                self.assertEqual(video_artifact(root, "short-v8-01")["clip"], "short-v8-01")
                item["render_name"] = "short-01"
                info["duration"] = 180.1
                with self.assertRaisesRegex(SystemExit, "180 seconds"):
                    video_artifact(root, "short-01")
                info["duration"] = 45
                item["candidate"]["title"] = "x" * 101
                with self.assertRaisesRegex(SystemExit, "1–100"):
                    video_artifact(root, "short-01")
            with self.assertRaisesRegex(SystemExit, "render basename"):
                video_artifact(root, "../token")

    def test_upload_requires_review(self):
        with patch("milloclips.youtube.connect") as connect:
            with self.assertRaisesRegex(SystemExit, "requires --approve"):
                upload_video(Path("unused"), "long", Path("unused"), False)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
