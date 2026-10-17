from __future__ import annotations

import json
import hashlib
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .ai import AI, thumbnail_copy, thumbnail_preferences
from .fonts import FILE as FONT
from .io import read_json, write_json
from .thumbnail_art import generate_artwork, plan_artwork


SIZE = (1280, 720)
CANDIDATES = 1
YELLOW = "#E9FF1F"
PATCH = "16.18.1"
DRAGON = "https://ddragon.leagueoflegends.com/cdn"
EXPRESSIONS = ("shocked", "hold-on", "overly-hyped", "smiling")
REFERENCES = Path(__file__).resolve().parent.parent / "assets" / "thumbnails"


def prepare_thumbnail_references(root: Path = REFERENCES) -> list[dict]:
    references = read_json(root / "references.json")
    output = root / "references"
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for reference in references:
        key = reference["id"]
        if len(key) != 11 or not all(char.isalnum() or char in "-_" for char in key):
            raise ValueError(f"Invalid YouTube reference id: {key}")
        path = output / f"{key}.jpg"
        if not path.exists():
            for quality in ("maxresdefault", "hqdefault"):
                try:
                    with urlopen(Request(f"https://i.ytimg.com/vi/{key}/{quality}.jpg", headers={"User-Agent": "milloclips/thumbnail-references"}), timeout=30) as response:
                        data = response.read()
                    with Image.open(BytesIO(data)) as source:
                        if source.width < 320:
                            continue  # YouTube's missing-thumbnail placeholder is 120px wide.
                        source.convert("RGB").save(path, quality=95)
                    break
                except HTTPError as error:
                    if error.code != 404:
                        raise
            if not path.exists():
                raise ValueError(f"No usable thumbnail for reference {key}; supply a screenshot")
        rows.append({**reference, "path": str(path.resolve())})
    sheet = Image.new("RGB", (960, 320 * ((len(rows) + 1) // 2)), "#10121b")
    for index, row in enumerate(rows):
        x, y = index % 2 * 480, index // 2 * 320
        with Image.open(row["path"]) as source:
            sheet.paste(ImageOps.fit(source, (480, 270)), (x, y + 40))
        ImageDraw.Draw(sheet).text((x + 10, y + 8), row["id"], font=ImageFont.truetype(FONT, 22), fill="white")
    sheet.save(output / "contact-sheet.jpg", quality=95)
    return rows


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


def _available_reactions(faces: Path) -> list[dict]:
    path = faces / "reactions.json"
    descriptions = read_json(path) if path.exists() else {}
    return [{"expression": name, "path": str(photo.resolve()), **descriptions.get(name, {})}
            for name in dict.fromkeys([*descriptions, *EXPRESSIONS])
            if name and all(char.isalnum() or char in "-_" for char in name)
            and (photo := _reaction_photo(faces, name)) is not None]


def _reaction(canvas: Image.Image, photo: Path | None, side: str = "right", crop: list[float] | None = None, mirrored: bool | None = None) -> None:
    if photo is None:
        return
    if not photo.exists() and photo.stem == "hyped":
        photo = photo.with_name(f"overly-hyped{photo.suffix}")  # Preserve old revision paths without rewriting raw records.
    width, height = 610, 720
    x, y = (670, 0) if side == "right" else (0, 0)
    with Image.open(photo) as source:
        face = source.convert("RGBA")
        if crop is not None:
            if len(crop) != 4 or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1):
                raise ValueError("Reaction framing requires a normalized crop inside the source")
            face = face.crop(tuple(round(value * (face.width if index % 2 == 0 else face.height)) for index, value in enumerate(crop)))
        else:
            face = face.crop(face.getbbox())
            face = face.crop((int(face.width * .12), 0, face.width, int(face.height * .82)))
        face = ImageOps.contain(face, (width, height), Image.Resampling.LANCZOS)
        zoom = 1.1 if crop else 1.5  # Head-specific crops already remove the unused torso.
        face = face.resize((round(face.width * zoom), round(face.height * zoom)), Image.Resampling.LANCZOS)
        if mirrored is True or (mirrored is None and side == "left"):
            face = ImageOps.mirror(face)
    # Outline the silhouette before pane clipping, never the rectangular crop edge.
    mask = face.getchannel("A")
    outlined = Image.new("RGBA", face.size, "white")
    outlined.putalpha(mask.filter(ImageFilter.MaxFilter(13)))
    outlined.alpha_composite(face)
    pixels = Image.new("RGBA", (width, height))
    paste = ((width - face.width) // 2, max(0, height - face.height))
    pixels.alpha_composite(outlined, paste)
    mask = pixels.getchannel("A")
    if canvas.mode == "RGBA":
        canvas.alpha_composite(pixels, (x, y))
    else:
        canvas.paste(pixels.convert("RGB"), (x, y), mask)


def _game_asset(output: Path, kind: str, key: str, force: bool = False) -> Path:
    if kind == "splash" and key.isalnum():
        url = f"{DRAGON}/img/champion/splash/{key}_0.jpg"
    elif kind == "champion" and key.isalnum():
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
    icon = output / f"asset-{kind}-{key}.{'jpg' if kind == 'splash' else 'png'}"
    if not icon.exists() or force:
        with urlopen(url, timeout=20) as response:
            data = response.read()
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG"} or min(image.size) < 32:
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
        icon_image = source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        canvas.paste(icon_image, (x, y), icon_image)


def _layout(frame: Path, concept: dict, photo: Path | None, assets: list[Path], splash: Path | None, number: int) -> Image.Image:
    if splash:
        with Image.open(splash) as source:
            canvas = ImageOps.fit(source.convert("RGB"), SIZE, centering=(.65, .4))
    else:
        canvas = _game_crop(frame)
        if number == 3:
            canvas = ImageOps.fit(canvas.crop((160, 90, 1120, 630)), SIZE)
    canvas = ImageEnhance.Color(canvas).enhance(1.25)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.15)
    side = "left" if number == 2 else "right"
    overlay = Image.new("RGBA", SIZE)
    shading = ImageDraw.Draw(overlay)
    for x in range(SIZE[0]):
        distance = x / SIZE[0] if side == "left" else 1 - x / SIZE[0]
        shading.line((x, 0, x, 720), fill=(6, 10, 20, int(155 * distance)))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    _reaction(canvas, photo, side)
    x = 635 if side == "left" else 45
    # Reserve separate headline and icon zones; never paint over the face.
    words = concept["headline"].split()
    middle = (len(words) + 1) // 2
    lines = [" ".join(words[:middle]), " ".join(words[middle:])] if len(words) > 1 else words
    draw = ImageDraw.Draw(canvas)
    for index, line in enumerate(filter(None, lines)):
        _text(draw, line, x, 35 + index * 125, 570, "white" if index == 0 else YELLOW)
    for index, icon in enumerate(assets[:2]):
        _badge(canvas, icon, x + 25 + index * 205, 470, 165)
    return canvas


def _artwork_prompt(concept: dict, direction: dict) -> str:
    gaze = "right, facing left" if direction["subject_side"] == "right" else "left, facing right"
    return (
        "Edit image 1 into ONE finished 16:9 Spanish League of Legends YouTube thumbnail. "
        "It contains ElmilloR's real reaction photo; integrate that person, preserving identity, expression, "
        f"headset and pose. Keep him on the {gaze}; do not mirror him again. "
        "One person only, one thin white silhouette outline; no duplicate head, offset likeness, "
        "human-shaped shadow or straight outline along photo crop edges. Remove the desktop mic if feasible.\n"
        "Layer order: cinematic game environment, reaction photo, headline ON TOP. Text may overlap lower torso, "
        "never eyes/mouth; every letter fully visible, no clipping or covering by the person.\n"
        f"Exact headline: {json.dumps(concept['headline'], ensure_ascii=False)}. "
        "Huge, clean bold type; solid white letters and one restrained accent color. Smooth solid fills: "
        "NO particles, speckles, cracks, distressing or texture inside letters. "
        "No underlines, red laser streaks, glowing highlight bars or light trails across/beneath text.\n"
        "Scene: " + direction["scene"] + "\n"
        "Image 2 is gameplay evidence; following game assets establish the correct subject. "
        "The LAST image is the original ElmilloR photo, the sole human identity reference. "
        "Use it for facial features, hair, expression and headset, not placement; image 1 sets orientation. "
        "Never blend another creator's face into him. "
        "Use supplied icons faithfully, only once. No invented stats, HUD, extra words or fake achievements. "
        "Return the finished thumbnail, not a layout board."
    )


def _generated_layout(ai: AI, output: Path, frame: Path, concept: dict, photo: Path, assets: list[Path], references: list[dict], budget: float) -> tuple[Image.Image, dict]:
    framing_path = photo.parent / "framing.json"
    framing = read_json(framing_path) if framing_path.exists() else {}
    preferences = thumbnail_preferences(ai.vod_dir)
    game_image = _game_crop(frame)
    game_path = output / f"game-{hashlib.sha256(game_image.tobytes()).hexdigest()[:16]}.jpg"
    if not game_path.exists():
        game_image.save(game_path, quality=95)
    direction = plan_artwork(ai, concept, references, game_path, photo, assets, preferences, output)
    protected = Image.new("RGBA", SIZE)
    _reaction(protected, photo, direction["subject_side"], framing.get(concept["expression"]))
    for asset, placement in zip(assets, direction["asset_positions"], strict=True):
        _badge(protected, asset, placement["x"], placement["y"], placement["size"])
    template = Image.alpha_composite(game_image.convert("RGBA"), protected)
    tag = hashlib.sha256(template.tobytes()).hexdigest()[:16]
    template_path, mask_path = output / f"layout-{tag}.png", output / f"mask-{tag}.png"
    template.save(template_path)
    # The finished image includes photo and text; allow the model to integrate both.
    mask = Image.new("RGBA", SIZE, (255, 255, 255, 0))
    mask.save(mask_path)
    art_reference = None
    if concept.get("champion", "").isalnum():
        art_reference = _game_asset(output, "splash", concept["champion"])
    inputs = [template_path, game_path]
    if art_reference:
        inputs.append(art_reference)
    # Style photos stay in Nano art direction; no competing human identities reach Sunburst.
    inputs += assets + [photo]
    prompt = _artwork_prompt(concept, direction)
    artwork = generate_artwork(ai, output, prompt, inputs, mask_path, budget)
    with Image.open(artwork) as source:
        canvas = source.convert("RGBA").resize(SIZE, Image.Resampling.LANCZOS)
    # Do not paste the photo again: that hides lettering and exposes offset copies.
    return canvas.convert("RGB"), {"artwork": str(artwork), "direction": direction, "template": str(template_path),
                                    "mask": str(mask_path), "face_crop": framing.get(concept["expression"]),
                                    "reaction_mirrored": direction["subject_side"] == "left",
                                    "composition_mode": "baked-photo", "reference_ids": [],
                                    "direction_reference_ids": direction.get("reference_ids", []),
                                    "exact_assets": [str(asset) for asset in assets]}


def _selected_frames(vod_dir: Path, plan: dict) -> tuple[list[dict], dict[int, Path]]:
    beats_data = read_json(vod_dir / "beats.json")
    beats = [beat for beat in beats_data["beats"] if beat["id"] in set(plan["selected_ids"])]
    if not beats:
        raise ValueError("The long plan has no selected beats for thumbnail evidence")
    inputs = vod_dir / "edit-input.json"
    analysis_dir = Path(read_json(inputs)["analysis_workspace"]) if inputs.exists() else vod_dir
    frames = sorted((analysis_dir / "frames" / "selected-game").glob("frame_*.jpg"))
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
    choices = (("ME PASÓ ESTO", "action", "shocked"), ("JUGUÉ ESTO", "identity", "hold-on"), ("ME VOLVIÓ LOCO", "action", "smiling"))
    return [
        {"title": headline.title(), "headline": headline, "hook": "evidenced final-edit moment",
         "template": template, "expression": expression, "beat_id": ordered[index % len(ordered)]["id"]}
        for index, (headline, template, expression) in enumerate(choices)
    ]


def _brief(vod_dir: Path, plan: dict, beats: list[dict], frames: dict[int, Path], ai: AI | None, force: bool, references: list[dict], reactions: list[dict]) -> list[dict]:
    path = vod_dir / "thumbnail-brief.json"
    key = {"version": 5, "game_id": plan["game_id"], "selected_ids": plan["selected_ids"], "story": plan.get("story", ""),
           "model": ai.editorial_model if ai else None, "mock": ai.mock if ai else None,
           "preferences": thumbnail_preferences(vod_dir),
           "evidence_signature": hashlib.sha256(json.dumps(beats, sort_keys=True).encode()
                                                 + b"".join(frame.read_bytes() for frame in frames.values())).hexdigest(),
           "reference_signature": hashlib.sha256(json.dumps(references, sort_keys=True).encode()
                                                  + b"".join(Path(row["path"]).read_bytes() for row in references)).hexdigest(),
           "reaction_signature": hashlib.sha256(json.dumps(reactions, sort_keys=True).encode()
                                                 + b"".join(Path(row["path"]).read_bytes() for row in reactions)).hexdigest()}
    if path.exists() and not force:
        cached = read_json(path)
        if all(cached.get(field) == value for field, value in key.items()):
            return cached["concepts"]
    ordered = sorted(beats, key=lambda beat: beat.get("importance", 0), reverse=True)
    # Include the spoken build/pick premise, not only high-importance fights.
    evidence = ordered[:12]
    evidence += [beat for beat in beats if beat not in evidence and beat.get("type") in {"STRATEGY", "SETUP", "REACTION"}][:4]
    concepts = ai.thumbnail_concepts(evidence, plan.get("story", ""), [frames[beat["id"]] for beat in evidence], force=force, references=references, reactions=reactions) if ai else []
    concepts = [concept for concept in concepts if concept.get("beat_id") in frames and concept.get("template") in {"action", "identity", "strategy"}]
    if not concepts:
        if ai and not ai.mock:
            raise ValueError("No grounded thumbnail concept; inspect thumbnail-concepts.raw.json and retry. No generic backfill was rendered.")
        concepts = (concepts + _fallback_concepts(beats))[:CANDIDATES]
    write_json(path, {**key, "concepts": concepts})
    return concepts


def render_thumbnails(vod_dir: Path, faces: Path = Path("assets/faces"), force: bool = False, ai: AI | None = None, image_budget: float = 5) -> Path:
    plan = read_json(vod_dir / "long-plan.json")
    output = vod_dir / "renders" / "thumbnails"
    output.mkdir(parents=True, exist_ok=True)
    beats, frames = _selected_frames(vod_dir, plan)
    references = prepare_thumbnail_references() if ai and not ai.mock else []
    reactions = _available_reactions(faces)
    if ai and not ai.mock and not reactions:
        raise ValueError(f"Add real reaction cutouts to {faces} before generating thumbnails")
    concepts = _brief(vod_dir, plan, beats, frames, ai, force, references, reactions)
    rows = []
    for number, concept in enumerate(concepts[:CANDIDATES], 1):
        concept = {**concept, "title": thumbnail_copy(concept["title"]).upper(), "headline": thumbnail_copy(concept["headline"])}
        photo = _reaction_photo(faces, concept.get("expression", ""))
        assets, splash = [], None
        if (not ai or ai.mock) and concept.get("champion", "").isalnum():
            if concept.get("background", "splash" if number == 2 else "gameplay") == "splash":
                splash = _game_asset(output, "splash", concept["champion"])
        for asset in concept.get("assets", [])[:6]:
            assets.append(_game_asset(output, asset["kind"], asset["key"]))
        if (not ai or ai.mock) and not splash and not assets and concept.get("champion", "").isalnum():
            assets.append(_game_asset(output, "champion", concept["champion"]))
        details = {}
        if ai and not ai.mock:
            if photo is None:
                raise ValueError(f"Missing real reaction cutout for {concept['expression']}; add it to {faces}")
            canvas, details = _generated_layout(ai, output, frames[concept["beat_id"]], concept, photo, assets, references, image_budget)
        else:
            canvas = _layout(frames[concept["beat_id"]], concept, photo, assets, splash, number)
        final = output / f"{number:02d}-{concept['template']}.png"
        canvas.save(final, optimize=True)
        rows.append({**concept, **details, "number": number, "background": details.get("artwork", str(splash or frames[concept["beat_id"]])), "frame": str(frames[concept["beat_id"]]),
                      "reaction_photo": str(photo) if photo else None, "render": str(final)})
    return _publish_candidates(vod_dir, rows)


def _contact_sheet(previews: list[Image.Image], rows: list[dict], width: int, font_size: int) -> Image.Image:
    font = ImageFont.truetype(FONT, font_size)
    captions = []
    for row in rows:
        lines = [f"{row['number']:02d} /"]
        for word in row["title"].split():
            line = f"{lines[-1]} {word}"
            if font.getlength(line) > width - 16:
                lines.append(word)
            else:
                lines[-1] = line
        captions.append("\n".join(lines))
    header = max(len(caption.splitlines()) for caption in captions) * (font_size + 4) + 16
    height = round(width * 9 / 16)
    sheet = Image.new("RGB", ((width + 10) * len(rows) + 10, header + height + 25), "#10121b")
    draw = ImageDraw.Draw(sheet)
    for index, (preview, caption) in enumerate(zip(previews, captions, strict=True)):
        x = 10 + index * (width + 10)
        sheet.paste(preview.resize((width, height), Image.Resampling.LANCZOS), (x, header))
        draw.multiline_text((x + 8, 8), caption, font=font, fill="white", spacing=4)
    return sheet


def _publish_candidates(vod_dir: Path, rows: list[dict]) -> Path:
    output = vod_dir / "renders" / "thumbnails"
    history = output / "revisions.reviewed.json"
    for correction in read_json(history) if history.exists() else []:
        result = correction.get("result_row")
        if not result or not Path(result["render"]).exists():
            continue
        index = correction["candidate"] - 1
        if hashlib.sha256(Path(rows[index]["render"]).read_bytes()).hexdigest() == correction.get("source_digest"):
            rows[index] = dict(result)
    sheet_path = Path(rows[0]["render"])
    write_json(output / "variants.json", rows)
    with Image.open(rows[0]["render"]) as image:
        image.save(vod_dir / "renders" / "thumbnail.png", optimize=True)
    write_json(output / "selection.json", {"status": "selected", "source": "generated", "winner": 1, "signature": _signature(rows)})
    return sheet_path


def revise_thumbnail(vod_dir: Path, candidate: int, edit: str, ai: AI, headline: str | None = None, image_budget: float = 5) -> Path:
    """One explicit edit call; retain the original artwork and recorded composition mode."""
    output = vod_dir / "renders" / "thumbnails"
    rows = read_json(output / "variants.json")
    candidate = 1
    if not rows or not edit.strip():
        raise ValueError("Generate a thumbnail and give a nonempty edit")
    row = dict(rows[candidate - 1])
    if not row.get("artwork") or not row.get("reaction_photo"):
        raise ValueError("Generate Sunburst candidates before revising")
    if headline is not None:
        headline = thumbnail_copy(headline.strip()).upper()
        if not headline or len(headline) > 40 or len(headline.split()) > 4:
            raise ValueError("Reviewed headline must contain 1-4 words and at most 40 characters")
        row["headline"] = headline
    assets = [Path(path) for path in row["exact_assets"]]
    protected = Image.new("RGBA", SIZE)
    _reaction(protected, Path(row["reaction_photo"]), row["direction"]["subject_side"], row.get("face_crop"), row.get("reaction_mirrored", False))
    for asset, position in zip(assets, row["direction"]["asset_positions"], strict=True):
        _badge(protected, asset, position["x"], position["y"], position["size"])
    baked = row.get("composition_mode") == "baked-photo"
    if baked:
        protected = Image.new("RGBA", SIZE)
    else:
        # Preserve legacy pixel-locked revisions in their recorded orientation.
        box = (0, 0, 616, 720) if row["direction"]["subject_side"] == "left" else (664, 0, 1280, 720)
        with Image.open(row["template"]) as original:
            protected.paste(original.convert("RGBA").crop(box), box[:2])
    with Image.open(row["render"]) as source:
        base = source.convert("RGBA")
    base.alpha_composite(protected)
    tag = hashlib.sha256(base.tobytes() + protected.tobytes()).hexdigest()[:16]
    base_path, mask_path = output / f"edit-base-{tag}.png", output / f"edit-mask-{tag}.png"
    base.save(base_path)
    mask = Image.new("RGBA", SIZE, "white")
    mask.putalpha(protected.getchannel("A"))
    mask.save(mask_path)
    prompt = (
        "Edit the FIRST thumbnail according to this specific feedback: " + edit.strip()
        + "\nKeep the composition and visual style except where the edit requests a change. "
        + ("Keep the single ElmilloR reaction's identity, expression, pose, inward gaze and headset. "
           "No second face, ghost copy or offset shadow. Headline is topmost, never hidden by the photo. " if baked else
           "The opaque mask protects the original human photo/icons, restored pixel-for-pixel. "
           "Do not move/redraw them or add people; remove any unprotected human copy. ")
        + "Solid smooth letters: no particles, speckles, cracks or distressed textures. No fake stats. "
        "The ONLY rendered words must be this exact headline: " + json.dumps(row["headline"], ensure_ascii=False)
    )
    history = output / "revisions.reviewed.json"
    corrections = read_json(history) if history.exists() else []
    previous = row["render"]
    correction = {"candidate": candidate, "source_render": previous, "source_digest": hashlib.sha256(Path(previous).read_bytes()).hexdigest(),
                  "edit": edit, "headline": row["headline"]}
    corrections.append(correction)
    write_json(history, corrections)
    artwork = generate_artwork(ai, output, prompt, [base_path, *assets], mask_path, image_budget)
    with Image.open(artwork) as source:
        canvas = source.convert("RGBA").resize(SIZE, Image.Resampling.LANCZOS)
    canvas.alpha_composite(protected)
    final = output / f"{candidate:02d}-revision-{artwork.stem.removeprefix('art-')}.png"
    canvas.convert("RGB").save(final, optimize=True)
    row.update(render=str(final), artwork=str(artwork), previous_render=previous, edit=edit)
    correction["result_row"] = row
    write_json(history, corrections)
    rows[candidate - 1] = row
    return _publish_candidates(vod_dir, rows)


def _signature(rows: list[dict]) -> str:
    return hashlib.sha256(b"".join(Path(row["render"]).read_bytes() for row in rows)
                          + json.dumps(rows, sort_keys=True).encode()).hexdigest()
