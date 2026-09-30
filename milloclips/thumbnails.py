from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .ai import AI
from .fonts import FILE as FONT
from .io import read_json, write_json


SIZE = (1280, 720)
YELLOW = "#E9FF1F"
PATCH = "16.18.1"
DRAGON = "https://ddragon.leagueoflegends.com/cdn"
EXPRESSIONS = ("shocked", "hold-on", "hyped", "smiling")


def _text(draw: ImageDraw.ImageDraw, value: str, x: int, y: int, width: int, color: str = "white") -> None:
    size = 112
    while size > 32:
        font = ImageFont.truetype(FONT, size)
        if draw.textbbox((0, 0), value, font=font, stroke_width=9)[2] <= width:
            break
        size -= 3
    draw.text((x + 7, y + 9), value, font=font, fill="#080b11")
    draw.text((x, y), value, font=font, fill=color, stroke_width=9, stroke_fill="#080b11")


def _reaction_photo(faces: Path, expression: str) -> Path | None:
    return next((faces / f"{expression}.{ext}" for ext in ("png", "jpg", "jpeg", "webp")
                 if (faces / f"{expression}.{ext}").is_file()), None)


def _reaction(canvas: Image.Image, photo: Path | None, side: str = "right") -> None:
    if photo is None:
        return
    width, height = 628, 720
    x, y = (815, 0) if side == "right" else (0, 0)
    with Image.open(photo) as source:
        face = ImageOps.contain(source.convert("RGBA"), (width, height), Image.Resampling.LANCZOS)
    pixels = Image.new("RGBA", (width, height))
    paste = ((width - face.width) // 2, height - face.height)
    pixels.alpha_composite(face, paste)
    mask = pixels.getchannel("A")
    rim = mask.filter(ImageFilter.MaxFilter(13))
    canvas.paste("white", (x, y), rim)
    canvas.paste(pixels.convert("RGB"), (x, y), mask)


def _game_asset(output: Path, kind: str, key: str, force: bool = False) -> Path:
    if kind == "champion" and key.isalnum():
        url = f"{DRAGON}/{PATCH}/img/champion/{key}.png"
    elif kind == "item" and key.isdecimal():
        url = f"{DRAGON}/{PATCH}/img/item/{key}.png"
    elif kind == "rune" and key.isdecimal():
        catalog = output / f"asset-runes-{PATCH}.json"
        if not catalog.exists() or force:
            with urlopen(f"{DRAGON}/{PATCH}/data/en_US/runesReforged.json", timeout=20) as response:
                catalog.write_bytes(response.read())
        styles = json.loads(catalog.read_text(encoding="utf-8"))
        rune = next((rune for style in styles for slot in style["slots"]
                     for rune in slot["runes"] if rune["id"] == int(key)), None)
        if rune is None or not rune["icon"].startswith("perk-images/"):
            raise ValueError(f"Unknown rune in patch {PATCH}: {key}")
        url = f"{DRAGON}/img/{rune['icon']}"
    else:
        raise ValueError(f"Invalid League icon: {kind} {key}")
    icon = output / f"asset-{kind}-{key}.png"
    if not icon.exists() or force:
        with urlopen(url, timeout=20) as response:
            data = response.read()
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or min(image.size) < 32:
                raise ValueError(f"Invalid League icon: {kind} {key}")
            image.verify()
        icon.write_bytes(data)
    return icon


def _game_crop(frame: Path) -> Image.Image:
    with Image.open(frame) as source:
        width, height = source.size
        # Keep game proof while removing the VOD's chat and facecam overlays.
        return ImageOps.fit(source.convert("RGB").crop((int(width * .23), 0, width, int(height * .74))), SIZE)


def _badge(canvas: Image.Image, icon: Path, x: int, y: int, size: int) -> None:
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((x - 12, y - 12, x + size + 12, y + size + 12), 18, fill="#10151c", outline=YELLOW, width=7)
    with Image.open(icon) as source:
        canvas.paste(source.convert("RGB").resize((size, size), Image.Resampling.NEAREST), (x, y))


def _layout(frame: Path, concept: dict, photo: Path | None, champion: Path | None) -> Image.Image:
    canvas = _game_crop(frame)
    draw = ImageDraw.Draw(canvas)
    template = concept["template"]
    if template == "identity" and champion:
        _badge(canvas, champion, 150, 300, 250)
        _text(draw, concept["headline"], 145, 145, 620, YELLOW)
    elif template == "strategy":
        _text(draw, concept["headline"], 145, 155, 620, YELLOW)
    else:
        _text(draw, concept["headline"], 145, 155, 620, YELLOW)
    _reaction(canvas, photo)
    return canvas


def _selected_frames(vod_dir: Path, plan: dict) -> tuple[list[dict], dict[int, Path]]:
    beats_data = read_json(vod_dir / "beats.json")
    beats = [beat for beat in beats_data["beats"] if beat["id"] in set(plan["selected_ids"])]
    if not beats:
        raise ValueError("The long plan has no selected beats for thumbnail evidence")
    frames = sorted((vod_dir / "frames" / "selected-game").glob("frame_*.jpg"))
    if not frames:
        raise FileNotFoundError("Missing cached selected-game frames")
    interval = float(beats_data.get("target_beat_seconds", 15)) / 3
    first = min(float(beat["start"]) for beat in beats_data["beats"])
    by_id = {
        beat["id"]: frames[min(len(frames) - 1, max(0, round((float(beat["start"]) - first) / interval)))]
        for beat in beats
    }
    return beats, by_id


def _fallback_concepts(beats: list[dict]) -> list[dict]:
    ordered = sorted(beats, key=lambda beat: (beat.get("importance", 0), beat.get("type") == "COMBAT"), reverse=True)
    choices = (("ME PASÓ ESTO", "action", "shocked"), ("JUGUÉ ESTO", "identity", "hold-on"), ("ME VOLVIÓ LOCO", "action", "hyped"))
    return [
        {"title": headline.title(), "headline": headline, "hook": "evidenced final-edit moment",
         "template": template, "expression": expression, "beat_id": ordered[index % len(ordered)]["id"]}
        for index, (headline, template, expression) in enumerate(choices)
    ]


def _brief(vod_dir: Path, plan: dict, beats: list[dict], frames: dict[int, Path], ai: AI | None, force: bool) -> list[dict]:
    path = vod_dir / "thumbnail-brief.json"
    key = {"game_id": plan["game_id"], "selected_ids": plan["selected_ids"], "story": plan.get("story", "")}
    if path.exists() and not force:
        cached = read_json(path)
        if all(cached.get(field) == value for field, value in key.items()):
            return cached["concepts"]
    evidence = sorted(beats, key=lambda beat: beat.get("importance", 0), reverse=True)[:12]
    concepts = ai.thumbnail_concepts(evidence, plan.get("story", ""), [frames[beat["id"]] for beat in evidence]) if ai else []
    concepts = [concept for concept in concepts if concept.get("beat_id") in frames and concept.get("template") in {"action", "identity", "strategy"}]
    if len(concepts) < 3:
        concepts = (concepts + _fallback_concepts(beats))[:3]
    write_json(path, {**key, "concepts": concepts})
    return concepts


def render_thumbnails(vod_dir: Path, faces: Path = Path("assets/faces"), force: bool = False, ai: AI | None = None) -> Path:
    plan = read_json(vod_dir / "long-plan.json")
    output = vod_dir / "renders" / "thumbnails"
    output.mkdir(parents=True, exist_ok=True)
    beats, frames = _selected_frames(vod_dir, plan)
    concepts = _brief(vod_dir, plan, beats, frames, ai, force)
    by_id = {beat["id"]: beat for beat in beats}
    rows, previews = [], []
    for number, concept in enumerate(concepts, 1):
        photo = _reaction_photo(faces, concept.get("expression", ""))
        champion = None
        if concept.get("champion", "").isalnum():
            champion = _game_asset(output, "champion", concept["champion"], force)
        canvas = _layout(frames[concept["beat_id"]], concept, photo, champion)
        score = int(by_id[concept["beat_id"]].get("importance", 1)) * 20 + len(concept["headline"].split()) * 3 + (8 if photo else 0)
        final = output / f"{number:02d}-{concept['template']}.png"
        canvas.save(final, optimize=True)
        previews.append(canvas.resize((480, 270)))
        rows.append({**concept, "score": score, "frame": str(frames[concept["beat_id"]]),
                     "reaction_photo": str(photo) if photo else None, "render": str(final)})
    best = max(rows, key=lambda row: row["score"])
    with Image.open(best["render"]) as image:
        image.save(vod_dir / "renders" / "thumbnail.png", optimize=True)
    sheet = Image.new("RGB", (1480, 350), "#10121b")
    draw = ImageDraw.Draw(sheet)
    for index, preview in enumerate(previews):
        x = 10 + index * 490
        sheet.paste(preview, (x, 55))
        draw.text((x + 8, 18), f"{index + 1:02d} / {rows[index]['score']} / {rows[index]['headline']}", font=ImageFont.truetype(FONT, 21), fill="white")
    sheet_path = output / "contact-sheet.jpg"
    sheet.save(sheet_path, quality=90)
    write_json(output / "variants.json", rows)
    return sheet_path
