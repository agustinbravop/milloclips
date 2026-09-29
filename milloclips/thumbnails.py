from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .fonts import FILE as FONT
from .io import read_json


SIZE = (1280, 720)
# ponytail: pinned to the Sep 23 VOD patch; choose patch by VOD date for other games.
PATCH = "16.18.1"
DRAGON = "https://ddragon.leagueoflegends.com/cdn"
VARIANTS = (
    ("teamfight", 250, "LA PELEA", "QUE LO CAMBIÓ TODO", "#ffe84f"),
    ("off-meta", 50, "FIDDLE", "¿SUPPORT?", "#c9ff45"),
    ("scoreboard", 440, "2 / 8 / 21", "Y AUN ASÍ GANÓ", "#ffe65b"),
    ("build", 330, "LA BUILD", "¿ESTÁ ROTA?", "#d5ff53"),
    ("missed-fear", 424, "FALLÉ", "EL FEAR A 3", "#ff88ca"),
    ("before-after", 250, "DE TROLL", "A WIN", "#6bffbb"),
)
REACTIONS = ("shocked", "hold-on", "shocked", "hyped", "hold-on", "smiling")


def _text(draw: ImageDraw.ImageDraw, value: str, x: int, y: int, width: int, color: str,
          size: int = 110, stroke: int = 8) -> None:
    while size > 32:
        font = ImageFont.truetype(FONT, size)
        if draw.textbbox((0, 0), value, font=font, stroke_width=stroke)[2] <= width:
            break
        size -= 3
    draw.text((x, y), value, font=font, fill=color, stroke_width=stroke, stroke_fill="#080b11")


def _reaction(canvas: Image.Image, photo: Path | None, side: str) -> None:
    # The large white-rimmed cutout follows the reference layouts. The silhouette
    # is an explicit empty slot until real streamer photos are supplied.
    width, height = 465, 610
    x = 815 if side == "right" else 0
    y = 110
    mask = Image.new("L", (width, height), 0)
    if photo:
        with Image.open(photo) as source:
            if source.mode == "RGBA" and source.getextrema()[3][0] < 255:
                face = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
                paste_x, paste_y = (width - face.width) // 2, height - face.height
                mask.paste(face.getchannel("A"), (paste_x, paste_y))
                pixels = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                pixels.paste(face, (paste_x, paste_y), face)
            else:
                pixels = ImageOps.fit(source.convert("RGBA"), (width, height), centering=(0.5, 0.25))
                ImageDraw.Draw(mask).rounded_rectangle((5, 5, width - 5, height + 50), radius=92, fill=255)
    else:
        pixels = Image.new("RGBA", (width, height), (16, 27, 31, 255))
        shape = ImageDraw.Draw(mask)
        shape.ellipse((105, 28, 360, 304), fill=255)
        shape.rounded_rectangle((32, 267, 433, 750), radius=170, fill=255)
        ImageDraw.Draw(pixels).ellipse((105, 28, 360, 304), fill="#25373b")
        ImageDraw.Draw(pixels).rounded_rectangle((32, 267, 433, 750), radius=170, fill="#25373b")
    rim = mask.filter(ImageFilter.MaxFilter(25))
    canvas.paste("white", (x, y), rim)
    canvas.paste(pixels.convert("RGB"), (x, y), mask)
    if not photo:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((x + 112, y + 320, x + 365, y + 388), 12, fill="white")
        draw.text((x + 135, y + 336), "TU REACCIÓN", font=ImageFont.truetype(FONT, 22), fill="#182029")


def _reaction_photo(faces: Path, expression: str) -> Path | None:
    """Select the expression that matches the thumbnail's editorial hook."""
    return next((faces / f"{expression}.{ext}" for ext in ("png", "jpg", "jpeg", "webp")
                 if (faces / f"{expression}.{ext}").is_file()), None)


def _panel(canvas: Image.Image, box: tuple[int, int, int, int], fill: str = "#101a1c") -> ImageDraw.ImageDraw:
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, 20, fill=fill, outline="white", width=5)
    return draw


def _item_badge(canvas: Image.Image, icon: Path | None, x: int, y: int, size: int) -> None:
    if icon is None:
        return
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((x - 12, y - 12, x + size + 12, y + size + 12), 18,
                           fill="#142128", outline="#ffdf70", width=7)
    with Image.open(icon) as source:
        artwork = source.convert("RGB").resize((size, size), Image.Resampling.NEAREST)
    canvas.paste(artwork, (x, y))


def _game_asset(output: Path, kind: str, key: str, force: bool = False) -> Path:
    """Download an icon only when a layout asks for it; keep all assets flat."""
    if kind == "item" and key.isdecimal():
        url = f"{DRAGON}/{PATCH}/img/item/{key}.png"
    elif kind == "champion" and key.isalnum():
        url = f"{DRAGON}/{PATCH}/img/champion/{key}.png"
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


def _mentioned_item(vod_dir: Path, plan: dict, output: Path, force: bool = False) -> Path | None:
    beats_path = vod_dir / "beats.json"
    if not beats_path.exists():
        return None
    selected = set(plan.get("selected_ids", []))
    if not any(beat["id"] in selected and "zeke" in beat.get("text", "").casefold()
               for beat in read_json(beats_path)["beats"]):
        return None
    return _game_asset(output, "item", "3050", force)


def _game_crop(frame: Path, target: tuple[int, int]) -> Image.Image:
    with Image.open(frame) as source:
        # Wide slice of the real game, excluding left-side chat and the webcam.
        width, height = source.size
        return ImageOps.fit(source.convert("RGB").crop((int(width * .23), 0, width, int(height * .74))), target)


def _layout(canvas: Image.Image, frame: Path, vod_dir: Path, number: int,
            photo: Path | None, item: Path | None, champion: Path | None) -> Image.Image:
    accent = VARIANTS[number - 1][4]
    canvas = canvas.convert("RGB")
    # Reserve room for the foreground face. Every layout still has Rift behind it.
    if number == 1:  # In-game fight, minimal copy like the first reference.
        canvas.paste(_game_crop(frame, (860, 720)), (0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, 860, 157), fill="#11191b")
        _text(draw, "¿CÓMO GANÓ?", 35, 22, 820, accent, 92)
        _reaction(canvas, photo, "right")
    elif number == 2:  # Champion spotlight, as in the loadout reference.
        draw = _panel(canvas, (35, 90, 762, 610), "#14252b")
        draw.rounded_rectangle((52, 109, 743, 593), 12, fill="#21383b")
        canvas.paste(_game_crop(frame, (670, 464)), (63, 119))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((35, 21, 773, 125), fill="#0c1518")
        _text(draw, "FIDDLE SUPPORT", 52, 15, 720, accent, 72)
        _item_badge(canvas, champion, 56, 441, 132)
        _item_badge(canvas, item, 597, 441, 132)
        _reaction(canvas, photo, "right")
    elif number == 3:  # The real 2/8/21 from the selected game.
        draw = _panel(canvas, (27, 82, 755, 606))
        _text(draw, "2 / 8 / 21", 51, 124, 680, accent, 105)
        draw.text((55, 284), "KILLS     MUERTES     ASIST.", font=ImageFont.truetype(FONT, 29), fill="white")
        draw.rounded_rectangle((53, 410, 711, 544), 12, fill="#ffe65b")
        _text(draw, "Y GANÓ", 76, 407, 595, "#142022", 96, 0)
        _item_badge(canvas, item, 599, 285, 83)
        _reaction(canvas, photo, "right")
    elif number == 4:  # Named build hook: actual Zeke icon, not an invented item.
        draw = _panel(canvas, (24, 59, 750, 622), "#142522")
        _text(draw, "FIDDLE SUPPORT", 48, 65, 677, accent, 70)
        _item_badge(canvas, item, 93, 221, 232)
        draw = ImageDraw.Draw(canvas)
        _text(draw, "ZEKE'S" if item else "LA BUILD", 370, 244, 358, "white", 83)
        _text(draw, "¿ROTO?", 67, 480, 640, accent, 89)
        _reaction(canvas, photo, "right")
    elif number == 5:  # The miss is the joke; gameplay remains visible.
        canvas.paste(_game_crop(frame, (860, 720)), (0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((22, 21, 779, 146), 16, fill="#241a2d", outline=accent, width=5)
        _text(draw, "FALLÉ EL FEAR", 42, 26, 715, "white", 90)
        draw.rounded_rectangle((35, 567, 741, 678), 15, fill=accent)
        _text(draw, "A TRES PERSONAS", 51, 574, 665, "#201526", 72, 0)
        _reaction(canvas, photo, "right")
    else:  # The user's preferred before/after hook, now on actual Rift frames.
        early = vod_dir / "frames" / "selected-game" / "frame_000185.jpg"
        late = vod_dir / "frames" / "selected-game" / "frame_000330.jpg"
        canvas.paste(_game_crop(early, (395, 565)), (17, 59))
        canvas.paste(_game_crop(late, (395, 565)), (423, 59))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((17, 59, 412, 624), outline="#ff747e", width=10)
        draw.rectangle((423, 59, 818, 624), outline="#6bffbb", width=10)
        draw.rounded_rectangle((38, 74, 285, 160), 10, fill="#ff747e")
        draw.rounded_rectangle((447, 74, 779, 160), 10, fill="#6bffbb")
        _text(draw, "ANTES", 55, 80, 218, "#12161b", 56, 0)
        _text(draw, "DESPUÉS", 464, 80, 300, "#12161b", 54, 0)
        draw.rectangle((0, 622, 1280, 720), fill="#11191b")
        _text(draw, "DE TROLL  →  WIN", 24, 626, 850, "white", 77)
        _item_badge(canvas, item, 648, 466, 110)
        _reaction(canvas, photo, "right")
    return canvas


def render_thumbnails(vod_dir: Path, faces: Path = Path("assets/faces"), force: bool = False) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    plan = read_json(vod_dir / "long-plan.json")
    if "809acd6b-dd4b-4660-b0ee-746f6398ad8e" not in manifest["metadata"]["input"] or plan["game_id"] != "game-002":
        raise ValueError("These thumbnail concepts are specific to the selected Fiddlesticks support game")
    output = vod_dir / "renders" / "thumbnails"
    output.mkdir(parents=True, exist_ok=True)
    old_icon = output / "zekes-16.18.1.png"
    if old_icon.exists() and not (output / "asset-item-3050.png").exists():
        old_icon.replace(output / "asset-item-3050.png")
    item = _mentioned_item(vod_dir, plan, output, force)
    champion = _game_asset(output, "champion", "Fiddlesticks", force)
    previews = []
    rows = []
    for number, (name, frame_number, title, subtitle, accent) in enumerate(VARIANTS, 1):
        frame = vod_dir / "frames" / "selected-game" / f"frame_{frame_number:06d}.jpg"
        if not frame.is_file():
            raise FileNotFoundError(f"Missing cached selected-game frame: {frame}")
        canvas = _game_crop(frame, SIZE)
        photo = _reaction_photo(faces, REACTIONS[number - 1])
        canvas = _layout(canvas, frame, vod_dir, number, photo, item, champion)
        final = output / f"{number:02d}-{name}.png"
        canvas.save(final, optimize=True)
        previews.append(canvas.resize((480, 270)))
        rows.append({"variant": number, "name": name, "title": title, "subtitle": subtitle,
                     "frame": str(frame), "reaction_photo": str(photo) if photo else None,
                     "item": "Zeke's Convergence" if item and number in (2, 3, 4, 6) else None,
                     "render": str(final)})
    sheet = Image.new("RGB", (1480, 690), "#10121b")
    draw = ImageDraw.Draw(sheet)
    for index, preview in enumerate(previews):
        x, y = 10 + index % 3 * 490, 28 + index // 3 * 340
        sheet.paste(preview, (x, y + 35))
        draw.text((x + 8, y), f"{index + 1:02d} / {VARIANTS[index][0].upper()}",
                  font=ImageFont.truetype(FONT, 21), fill="white")
    sheet_path = output / "contact-sheet.jpg"
    sheet.save(sheet_path, quality=90)
    (output / "variants.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for old in output.glob("raw-*.png"):
        old.unlink()
    old_icon.unlink(missing_ok=True)
    for name in ("01-comeback", "02-support", "03-item", "04-fear", "05-ambush", "06-reversal"):
        (output / f"{name}.png").unlink(missing_ok=True)
    (vod_dir / "renders" / "thumbnail.png").unlink(missing_ok=True)
    return sheet_path
