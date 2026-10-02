from __future__ import annotations

import os
import re
import shutil
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from .editing import validated_short_renders
from .io import read_json, write_json
from .media import media_info, run_ffmpeg


LONG_WIDTH, LONG_HEIGHT = 1280, 720
SHORT_WIDTH, SHORT_HEIGHT = 1080, 1920


def slugify(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", plain.casefold()).strip("-")
    return slug or "stream"


def kick_path_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/videos/([0-9a-fA-F-]+)", url)
    return match.group(1) if match else None


def match_vod(catalog: dict, kick_id: str | None, vod_id: str | None) -> dict | None:
    vods = catalog["vods"]
    if vod_id is not None:
        exact = [vod for vod in vods if vod["id"] == vod_id]
        if exact:
            return exact[0]
        matches = [vod for vod in vods if vod["id"].startswith(vod_id)]
        return matches[0] if len(matches) == 1 else None
    if kick_id is not None:
        for vod in vods:
            if kick_path_id(vod.get("kickUrl")) == kick_id:
                return vod
    return None


def thumbnail_selection(vod_dir: Path) -> tuple[str | None, Path | None]:
    directory = vod_dir / "renders" / "thumbnails"
    variants_path = directory / "variants.json"
    selection_path = directory / "selection.json"
    poster = vod_dir / "renders" / "thumbnail.png"
    if not poster.exists():
        return None, None
    if not variants_path.exists():
        return None, poster
    rows = read_json(variants_path)
    selection = read_json(selection_path) if selection_path.exists() else {}
    winner = selection.get("winner")
    if selection.get("status") != "selected" or type(winner) is not int or not 1 <= winner <= len(rows):
        winner = 1
    title = str(rows[winner - 1].get("title", "")).strip() or None
    return title, poster


def _hardlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _poster(video: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    run_ffmpeg(["-ss", "0.5", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(destination)])


def _long_clip(vod_dir: Path, vod: dict, slug: str, title: str, youtube: str | None, static: Path) -> dict:
    render = vod_dir / "renders" / "long-rough.mp4"
    if render.exists():
        info = media_info(render)
        if (info["width"], info["height"]) != (LONG_WIDTH, LONG_HEIGHT) or not info["audio"]:
            raise SystemExit(f"Long render is not a valid {LONG_WIDTH}x{LONG_HEIGHT} video with audio: {render}")
        duration = round(info["duration"], 3)
    elif youtube:
        duration = round(float(read_json(vod_dir / "long-plan.json")["duration"]), 3)
    else:
        raise SystemExit("renders/long-rough.mp4 is missing; render the long form or pass --youtube")
    poster = vod_dir / "renders" / "thumbnail.png"
    if not poster.exists():
        raise SystemExit("renders/thumbnail.png is missing; run the thumbnail process before publishing")
    image = static / "images" / f"{slug}-thumbnail.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(poster, image)
    if youtube:
        video = {"provider": "youtube", "src": youtube}
    else:
        media = static / "media" / f"{slug}-long.mp4"
        _hardlink(render, media)
        video = {"provider": "file", "src": f"/media/{media.name}"}
    return {"id": f"{vod['id']}-long", "title": title, "poster": f"/images/{image.name}",
            "duration": duration, "width": LONG_WIDTH, "height": LONG_HEIGHT, "video": video}


def _short_clips(vod_dir: Path, vod: dict, slug: str, static: Path) -> list[dict]:
    if not (vod_dir / "short-candidates.json").exists():
        return []
    clips = []
    for index, render in enumerate(validated_short_renders(vod_dir), 1):
        candidate = render["candidate"]
        name = f"{slug}-short-{index:02d}"
        media = static / "media" / f"{name}.mp4"
        _hardlink(render["render"], media)
        poster = static / "images" / f"{name}.jpg"
        _poster(render["render"], poster)
        title = str(candidate.get("title", "")).strip() or f"{vod['title']} short {index}"
        clips.append({"id": f"{vod['id']}-short-{index:02d}", "title": title,
                      "poster": f"/images/{poster.name}", "duration": render["duration"],
                      "width": SHORT_WIDTH, "height": SHORT_HEIGHT,
                      "video": {"provider": "file", "src": f"/media/{media.name}"}})
    return clips


def publish(vod_dir: Path, site: Path, approve: bool, youtube: str | None = None,
            slug: str | None = None, vod_id: str | None = None) -> Path:
    if not approve:
        raise SystemExit("publish requires --approve")
    static = site / "static"
    catalog_path = static / "catalog.json"
    catalog = read_json(catalog_path)
    manifest = read_json(vod_dir / "manifest.json")
    kick_id = kick_path_id((manifest.get("metadata") or {}).get("input"))
    vod = match_vod(catalog, kick_id, vod_id)
    if vod is None:
        raise SystemExit("No matching VOD in the catalog; add it with the vods workflow or pass --vod-id")

    generated_title, _ = thumbnail_selection(vod_dir)
    slug = slugify(slug or generated_title or vod["title"])

    long_clip = None
    if (vod_dir / "renders" / "long-rough.mp4").exists() or youtube:
        long_clip = _long_clip(vod_dir, vod, slug, generated_title or vod["title"], youtube, static)
    shorts = _short_clips(vod_dir, vod, slug, static)
    if long_clip is None and not shorts:
        raise SystemExit("Nothing to publish: no long render and no validated shorts")
    if long_clip is not None:
        vod["long"] = long_clip
    vod["shorts"] = shorts
    vod.pop("pendingReason", None)
    catalog["generatedAt"] = datetime.now(UTC).strftime("%Y-%m-%d")
    write_json(catalog_path, catalog)
    return catalog_path


def publish_confirmed(vod_dir: Path, site: Path, state: dict, vod_id: str | None = None) -> Path:
    if not state.get("confirmed") or state.get("privacy") != "unlisted":
        raise SystemExit("Only confirmed Unlisted uploads can be linked to the gallery.")
    artifact = state["artifact"]
    name = re.sub(r"^short-v\d+-", "short-", artifact["clip"])
    catalog_path = site / "static" / "catalog.json"
    catalog = read_json(catalog_path)
    manifest = read_json(vod_dir / "manifest.json")
    vod = match_vod(catalog, kick_path_id((manifest.get("metadata") or {}).get("input")), vod_id)
    if vod is None:
        raise SystemExit("No matching catalog VOD; run catalog-sync or pass --vod-id.")
    # Catalog-id basenames stay stable when titles change and cannot collide across VODs.
    basename = slugify(f"{vod['id']}-{name}")
    image = site / "static" / "images" / f"{basename}.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    if name == "long":
        shutil.copy2(vod_dir / "youtube" / "long-thumbnail.jpg", image)
    else:
        _poster(Path(artifact["path"]), image)
    clip = {"id": f"{vod['id']}-{name}", "title": artifact["title"], "poster": f"/images/{image.name}",
            "duration": artifact["duration"], "width": artifact["width"], "height": artifact["height"],
            "video": {"provider": "youtube", "src": state["video_id"]}}
    if name == "long":
        vod["long"] = clip
    else:
        shorts = vod.setdefault("shorts", [])
        index = next((index for index, item in enumerate(shorts) if item["id"] == clip["id"]), None)
        if index is None:
            shorts.append(clip)
        else:
            shorts[index] = clip
    vod.pop("pendingReason", None)
    catalog["generatedAt"] = datetime.now(UTC).strftime("%Y-%m-%d")
    write_json(catalog_path, catalog)
    return catalog_path
