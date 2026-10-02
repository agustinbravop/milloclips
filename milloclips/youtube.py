from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from oauthlib.oauth2 import OAuth2Error
from PIL import Image
from requests import RequestException

from .editing import validated_short_renders
from .io import read_json, write_json
from .media import media_info
from .publish import LONG_HEIGHT, LONG_WIDTH, thumbnail_selection


API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CHUNK_SIZE = 8 * 1024 * 1024


def _save_token(path: Path, credentials: Credentials) -> None:
    write_json(path, json.loads(credentials.to_json()))
    path.chmod(0o600)


def _check(response):
    if response.status_code >= 400:
        try:
            reasons = [item["reason"] for item in response.json().get("error", {}).get("errors", [])]
        except (ValueError, KeyError, TypeError):
            reasons = []
        reason = ", ".join(reasons) or f"HTTP {response.status_code}"
        if "uploadLimitExceeded" in reasons:
            raise SystemExit("YouTube daily channel upload limit reached. Progress saved; rerun later.")
        if "quotaExceeded" in reasons:
            raise SystemExit("YouTube API quota reached. Progress saved; rerun after quota resets.")
        # Never print response bodies: resumable URLs and OAuth details are private.
        raise SystemExit(f"YouTube request failed ({reason}). Progress saved; rerun to resume.")
    return response


def _channel(session, expected: str) -> dict:
    result = _check(session.get(f"{API}/channels", params={"part": "snippet", "mine": "true"}, timeout=60)).json()
    channels = result.get("items", [])
    if len(channels) != 1:
        raise SystemExit("Authorize exactly one YouTube channel; no matching personal channel was returned.")
    channel = channels[0]
    handle = channel["snippet"].get("customUrl", "")
    if expected not in (channel["id"], handle) and expected.lstrip("@").casefold() != handle.lstrip("@").casefold():
        raise SystemExit(f"Wrong YouTube channel: expected {expected}, got {handle or channel['id']}.")
    return {"id": channel["id"], "handle": handle, "title": channel["snippet"]["title"]}


def authorize(client_secrets: Path, auth_dir: Path, expected: str) -> dict:
    if not client_secrets.is_file() or "installed" not in read_json(client_secrets):
        raise SystemExit("Provide an existing Desktop app OAuth client JSON via --client-secrets.")
    auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_dir.chmod(0o700)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    try:
        credentials = flow.run_local_server(host="localhost", port=0, access_type="offline", prompt="consent",
                                            authorization_prompt_message="Complete YouTube authorization in your browser.")
        with AuthorizedSession(credentials) as session:
            channel = _channel(session, expected)
    except (OAuth2Error, RequestException):
        raise SystemExit("Google authorization failed. Check the test user, API enablement and consent, then retry.") from None
    _save_token(auth_dir / "token.json", credentials)
    write_json(auth_dir / "channel.json", channel)
    return channel


def connect(auth_dir: Path):
    token = auth_dir / "token.json"
    if not token.exists() or not (auth_dir / "channel.json").exists():
        raise SystemExit("Run milloclips youtube-auth --client-secrets <desktop-client.json> first.")
    credentials = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not credentials.valid:
        try:
            credentials.refresh(Request())
        except Exception:
            raise SystemExit("YouTube authorization expired or was revoked. Run youtube-auth again.") from None
        _save_token(token, credentials)
    session = AuthorizedSession(credentials)
    channel = read_json(auth_dir / "channel.json")
    try:
        _channel(session, channel["id"])
    except RequestException:
        session.close()
        raise SystemExit("Could not verify your YouTube channel. Retry later.") from None
    except SystemExit:
        session.close()
        raise
    return session, channel


def _digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def video_artifact(vod_dir: Path, clip: str) -> dict:
    if clip != "long" and not re.fullmatch(r"short-(?:v\d+-)?\d{2,}", clip):
        raise SystemExit("--clip must be long or a render basename such as short-01.")
    if clip == "long":
        video = vod_dir / "renders" / "long-rough.mp4"
        title, thumbnail = thumbnail_selection(vod_dir)
        if not title or thumbnail is None:
            raise SystemExit("Long upload requires a generated title and renders/thumbnail.png.")
    else:
        matches = [item for item in validated_short_renders(vod_dir) if item["render_name"] == clip]
        if len(matches) != 1:
            raise SystemExit(f"No validated short render named {clip}; use its renders/<name>.mp4 basename.")
        video = matches[0]["render"]
        title = str(matches[0]["candidate"].get("title", "")).strip()
        thumbnail = None
    if not title or len(title) > 100 or any(char in title for char in "<>"):
        raise SystemExit("Pipeline title must contain 1–100 characters and no < or >. Correct the artifact first.")
    if not video.exists():
        raise SystemExit(f"Missing rendered video: {video}")
    info = media_info(video)
    dimensions = (LONG_WIDTH, LONG_HEIGHT) if clip == "long" else (1080, 1920)
    if (info["width"], info["height"]) != dimensions or not info["audio"] or info["duration"] <= 0:
        raise SystemExit(f"Invalid {clip} render: expected {dimensions[0]}x{dimensions[1]} with audio.")
    if clip != "long" and info["duration"] > 180:
        raise SystemExit("YouTube Shorts must be no longer than 180 seconds.")
    metadata = read_json(vod_dir / "manifest.json").get("metadata") or {}
    source = metadata.get("input", "")
    description = f"ElmilloR · edición con IA\nFuente: {source}" if source.startswith("https://kick.com/") else "ElmilloR · edición con IA"
    return {"clip": clip, "path": str(video.resolve()), "sha256": _digest(video), "size": video.stat().st_size,
            "title": title, "description": description, "duration": round(info["duration"], 3),
            "width": info["width"], "height": info["height"],
            "thumbnail_sha256": _digest(thumbnail) if thumbnail else None}


def _thumbnail(vod_dir: Path, destination: Path) -> None:
    with Image.open(vod_dir / "renders" / "thumbnail.png") as image:
        image = image.convert("RGB")
        image.thumbnail((1280, 720))
        image.save(destination, "JPEG", quality=90)
    if destination.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit("Upload thumbnail exceeds YouTube's 2 MB limit.")


def _resume_video(session, state: dict, state_path: Path) -> None:
    artifact = state["artifact"]
    size = artifact["size"]
    if not state.get("session_url"):
        response = _check(session.post(f"{UPLOAD}/videos", params={"uploadType": "resumable", "part": "snippet,status",
                                                                  "notifySubscribers": "false"},
            json={"snippet": {"title": artifact["title"], "description": artifact["description"], "categoryId": "20",
                              "defaultLanguage": "es"},
                  "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "embeddable": True}},
            headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}, timeout=60))
        state["session_url"] = response.headers["Location"]
        write_json(state_path, state)
    response = session.put(state["session_url"], headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
                           data=b"", timeout=120)
    if response.status_code in (404, 410):
        raise SystemExit("Saved upload session expired. Check Studio for this video before manually removing its state; no replacement upload started.")
    with Path(artifact["path"]).open("rb") as video:
        while response.status_code == 308:
            offset = int(response.headers["Range"].rsplit("-", 1)[1]) + 1 if "Range" in response.headers else 0
            if offset < 0 or offset >= size:
                raise SystemExit("Unexpected YouTube upload offset. State retained for recovery.")
            video.seek(offset)
            data = video.read(CHUNK_SIZE)
            response = session.put(state["session_url"], data=data,
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                         "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"}, timeout=120)
    result = _check(response).json()
    state["video_id"] = result["id"]
    state["url"] = f"https://www.youtube.com/watch?v={result['id']}"
    state["privacy"] = result.get("status", {}).get("privacyStatus")
    state.pop("session_url", None)
    write_json(state_path, state)


def upload_video(vod_dir: Path, clip: str, auth_dir: Path, approve: bool) -> dict:
    if not approve:
        raise SystemExit("YouTube upload requires --approve after reviewing this individual video.")
    artifact = video_artifact(vod_dir, clip)
    state_path = vod_dir / "youtube" / f"{clip}.json"
    # ponytail: serial uploads only; add a per-clip file lock if concurrent uploads are needed.
    state = read_json(state_path) if state_path.exists() else {"artifact": artifact}
    if state["artifact"] != artifact:
        raise SystemExit("This video's artifacts changed since upload started. Existing upload retained; review before replacing it.")
    session, channel = connect(auth_dir)
    try:
        if state.get("channel_id", channel["id"]) != channel["id"]:
            raise SystemExit("Saved upload belongs to a different YouTube channel.")
        state["channel_id"] = channel["id"]
        write_json(state_path, state)
        if not state.get("video_id"):
            _resume_video(session, state, state_path)
        if clip == "long" and not state.get("thumbnail_uploaded"):
            thumbnail = state_path.parent / "long-thumbnail.jpg"
            _thumbnail(vod_dir, thumbnail)
            with thumbnail.open("rb") as file:
                _check(session.post(f"{UPLOAD}/thumbnails/set", params={"videoId": state["video_id"], "uploadType": "media"},
                                    data=file, headers={"Content-Type": "image/jpeg"}, timeout=120))
            state["thumbnail_uploaded"] = True
            write_json(state_path, state)
        return state
    except RequestException:
        raise SystemExit("YouTube connection interrupted. Rerun the same command to resume the saved upload.") from None
    finally:
        session.close()


def confirm_video(vod_dir: Path, clip: str, auth_dir: Path, approve: bool) -> dict:
    if not approve:
        raise SystemExit("Gallery confirmation requires --approve after checking Unlisted playback.")
    artifact = video_artifact(vod_dir, clip)
    state_path = vod_dir / "youtube" / f"{clip}.json"
    if not state_path.exists():
        raise SystemExit("No saved YouTube upload for this clip.")
    state = read_json(state_path)
    if state["artifact"] != artifact:
        raise SystemExit("Current artifacts differ from the uploaded video. Restore them before linking this upload.")
    if not state.get("video_id") or (clip == "long" and not state.get("thumbnail_uploaded")):
        raise SystemExit("Upload is incomplete; rerun --upload first.")
    session, channel = connect(auth_dir)
    try:
        result = _check(session.get(f"{API}/videos", params={"part": "snippet,status,processingDetails", "id": state["video_id"]}, timeout=60)).json()
        items = result.get("items", [])
        if len(items) != 1:
            raise SystemExit("Uploaded video is missing or inaccessible.")
        video = items[0]
        if video["snippet"]["channelId"] != channel["id"] or state["channel_id"] != channel["id"]:
            raise SystemExit("Uploaded video belongs to a different channel.")
        status = video["status"]
        if status.get("privacyStatus") != "unlisted":
            raise SystemExit("Video is not Unlisted. Change visibility in YouTube Studio, then rerun confirmation.")
        if status.get("uploadStatus") != "processed" or video.get("processingDetails", {}).get("processingStatus") != "succeeded":
            raise SystemExit("Video is not successfully processed yet. Check Studio and retry later.")
        if not status.get("embeddable"):
            raise SystemExit("Enable embedding in YouTube Studio before linking the gallery.")
        state["privacy"] = "unlisted"
        state["confirmed"] = True
        write_json(state_path, state)
        return state
    except RequestException:
        raise SystemExit("Could not check YouTube status. Retry confirmation later.") from None
    finally:
        session.close()
