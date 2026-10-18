"""Reference-driven artwork; exact photos/icons are restored by the compositor."""
from __future__ import annotations

import base64
import hashlib
import json
import math
from io import BytesIO
from pathlib import Path

from PIL import Image
from openai import BadRequestError

from .ai import AI
from .io import read_json, write_json


MODEL = "gpt-image-2.5-sunburst"
SETTINGS = {"model": MODEL, "quality": "high", "size": "1536x864", "output_format": "png", "n": 1}
# Admission estimate, not a provider-enforced cap. Unknown/failed calls retain
# their reservation so retries cannot silently multiply experiment spending.
RESERVATION_USD = 1.20


def _key(value: dict, images: list[Path]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()
                          + b"".join(image.read_bytes() for image in images)).hexdigest()[:24]


def image_cost(usage: dict | None) -> float | None:
    if not usage or "output_tokens" not in usage or "input_tokens_details" not in usage:
        return None
    details = usage["input_tokens_details"]
    if not {"text_tokens", "image_tokens"} <= details.keys():
        return None
    return (details["text_tokens"] * 5 + details["image_tokens"] * 8 + usage["output_tokens"] * 30) / 1_000_000


def generate_artwork(ai: AI, output: Path, prompt: str, images: list[Path], mask: Path, budget: float = 5) -> Path:
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("Thumbnail image budget must be finite and positive")
    if not 1 <= len(images) <= 16:
        raise ValueError("Sunburst needs 1-16 reference images")
    with Image.open(images[0]) as source, Image.open(mask) as protection:
        if source.size != protection.size or "A" not in protection.getbands():
            raise ValueError("Artwork mask must have alpha and match the first image")
    request = {**SETTINGS, "prompt": prompt, "mock": ai.mock}
    signature = _key(request, [*images, mask])
    artwork = output / f"art-{signature}.png"
    record = artwork.with_suffix(".raw.json")
    if artwork.exists() and record.exists():
        with Image.open(artwork) as source:
            source.verify()
        return artwork
    output.mkdir(parents=True, exist_ok=True)
    write_json(artwork.with_suffix(".request.json"), {**request, "images": [str(path) for path in images], "mask": str(mask)})
    if ai.mock:
        with Image.open(images[0]) as source:
            source.convert("RGB").save(artwork)
        write_json(record, {"mock": True, "cost_usd": 0})
        return artwork
    ledger_path = output / "image-usage.json"
    ledger = read_json(ledger_path) if ledger_path.exists() else {"requests": []}
    spent = sum(row["cost_usd"] for row in ledger["requests"])
    if spent + RESERVATION_USD > budget:
        raise ValueError(f"Thumbnail image budget exhausted: ${spent:.2f} spent/reserved; next call reserves ${RESERVATION_USD:.2f}, budget ${budget:.2f}")
    entry = {"signature": signature, "model": MODEL, "status": "pending", "cost_usd": RESERVATION_USD, "estimated": True}
    ledger["requests"].append(entry)
    write_json(ledger_path, ledger)
    try:
        # Disable SDK retries: a timed-out generation may already be billable.
        client = ai.client.with_options(max_retries=0)
        response = client.images.edit(
            **SETTINGS, prompt=prompt,
            image=[(path.name, path.read_bytes(), "image/png" if path.suffix.lower() == ".png" else "image/jpeg") for path in images],
            mask=(mask.name, mask.read_bytes(), "image/png"), timeout=300,
        )
        usage = response.usage.model_dump() if response.usage else None
        cost = image_cost(usage)
        if cost is not None:
            entry.update(cost_usd=cost, estimated=False)
        write_json(record, {"model": MODEL, "usage": usage, "cost_usd": cost, "created": response.created,
                            "revised_prompt": response.data[0].revised_prompt if response.data else None})
        if not response.data or not response.data[0].b64_json:
            raise ValueError("Sunburst returned no image; inspect the raw response record")
        data = base64.b64decode(response.data[0].b64_json, validate=True)
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG" or source.width < 640 or abs(source.width / source.height - 16 / 9) > .02:
                raise ValueError("Sunburst returned an invalid or non-16:9 artwork")
            source.verify()
        artwork.write_bytes(data)
        entry["status"] = "complete"
        with (ai.vod_dir / "usage.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"stage": "thumbnail-artwork", "model": MODEL, "usage": usage, "cost_usd": cost}) + "\n")
        return artwork
    except Exception as error:
        entry["status"] = "failed"
        entry["error_type"] = type(error).__name__
        if isinstance(error, BadRequestError):
            # A request rejected during validation did not generate an image.
            entry.update(cost_usd=0, estimated=False, error_code=error.code)
        raise
    finally:
        write_json(ledger_path, ledger)


def plan_artwork(ai: AI, concept: dict, references: list[dict], frame: Path, photo: Path, assets: list[Path], preferences: str, output: Path, subject_reference: Path | None = None) -> dict:
    instructions = (
        "Art-direct this evidence-backed ElmilloR League YouTube thumbnail. The thumbnail is a single coherent visual idea."
        "A large real-photo reaction is essential. Match emotion to the actual hook without overexaggerating. "
        "Prefer subject_side RIGHT. Source photos already face LEFT, so keep them unflipped on the right. "
        "On the left they are mirrored to face RIGHT. Use the left only when this "
        "specific composition has a reason, and explain that in placement_reason. Never turn the person "
        "outward or invent a new face/pose. Keep main game action in the opposite half. "
        "Headline is topmost and may overlap the lower torso, never eyes/mouth; no letters hidden by the photo. "
        "Relevant exact icons can overlap lower torso, never eyes/mouth. "
        "Typography is stable, bold and condensed, with a straight baseline and consistent letter shapes. "
        "Use word size, restrained color, clean outlines and modest depth for emphasis; no playful/wobbly "
        "fonts, bouncing letters, individual rotations, warping or curved baselines. "
        "Controlled game-themed finish, not playful letterforms: subtle gradient, shallow bevel and faint "
        "grain/worn texture are welcome. Keep interiors mostly solid and readable at feed size; no heavy distressing or cracks. "
        "Allow at most ONE localized glow or sparse particle effect around an emphasized word, matching the subject; "
        "keep supporting words cleaner. No stacked effects or particle showers. "
        "Avoid chaotic/aggressive distressing or dominant cartoon type."
        "Write a concrete 60-90-word scene direction: environment, game subject, camera, light, type and hierarchy. "
        "Use game-faithful cinematic framing or enlarged champion silhouettes on the Rift, or a simple closer Rift "
        "background with a killer headline and optional champions in action. Do not force a champion into every concept. Never recreate another creator. "
        "Title, headline, subject and reaction must deliver ONE cohesive promise. If the copy names a champion, "
        "show that champion clearly and prominently. Use the supplied official subject artwork when gameplay "
        "obscures them; do not omit the named subject or replace it with scenery. "
        "Every League champion, skin, character, weapon, monster and spell must closely match an actual supplied "
        "visual reference: exact assets or clearly visible gameplay. Preserve silhouette, anatomy, proportions, "
        "colors, clothing, equipment and spell shapes. Do not design from names or model memory. "
        "If a monster, spell or other detail is not clearly referenced, omit it and simplify the scene; "
        "a tiny ambiguous screenshot or another creator's stylization is not a design reference. "
        "Champion/role/session-led gameplay packaging is valid without an amazing moment; never illustrate a failure "
        "as the selling point. Standard core items need no spotlight; up to 2-3 exact icons can explain a distinctive build. "
        "No invented damage numbers, kill banners, full builds, or game stats. Artistic champion staging is illustration, "
        "not evidence that a play occurred. Explain why this title/headline pair deserves a click; "
        "viewer_question may be empty for honest champion-led gameplay without a specific curiosity gap. "
        'Return JSON {"subject_side":"right","placement_reason":"...","scene":"complete narrative art direction",'
        '"viewer_question":"...","reference_ids":["id"],"asset_positions":[{"x":400,"y":480,"size":160}]}.'
        " Coordinates are integer pixels on 1280x720; sizes 80-220, all assets fully inside canvas. "
        "Exact safe bounds: 12 <= x <= 1268-size and 12 <= y <= 708-size. "
        "For icons above y=420, keep x+size <= 670 when subject_side is right, "
        "or x >= 610 when subject_side is left, so icons cannot obscure the reaction. "
        "One position per supplied exact icon, in supplied order. No positions if there are no icons. "
        "Choose only 1-2 real reference_ids that best fit this concept, not the entire library or placeholders. "
        "First image is gameplay evidence, second the real reaction, "
        + ("third the official subject artwork (strict character-design reference), " if subject_reference else "")
        + "then exact icons, then style examples. "
        "Style examples teach composition only; do not carry their champions, people, text, or stats into this video. "
        "Concept: " + json.dumps(concept, ensure_ascii=False) + "\nChannel/video guidance: " + preferences
        + "\nExact icons: " + json.dumps([path.name for path in assets])
        + "\nStyle examples: " + json.dumps(references, ensure_ascii=False)
    )
    images = [frame, photo, *([subject_reference] if subject_reference else []), *assets, *[Path(reference["path"]) for reference in references]]
    signature = _key({"instructions": instructions, "model": ai.editorial_model, "mock": ai.mock}, images)
    path = output / f"direction-{signature}.raw.json"
    if path.exists():
        direction = read_json(path)
    else:
        direction = ai._json(instructions, images, model=ai.editorial_model, detail="high")
        write_json(path, direction)
    reviewed_path = output / f"direction-{signature}.reviewed.json"
    if reviewed_path.exists():
        direction = read_json(reviewed_path)
    if direction.get("subject_side") not in {"left", "right"} or not isinstance(direction.get("scene"), str) or not direction["scene"].strip():
        raise ValueError(f"Invalid thumbnail art direction: {path}")
    positions = direction.get("asset_positions", [])
    if not isinstance(positions, list) or len(positions) != len(assets):
        raise ValueError(f"One placement is required per exact asset: {path}")
    for position in positions:
        if not isinstance(position, dict) or any(type(position.get(field)) is not int for field in ("x", "y", "size")):
            raise ValueError(f"Invalid thumbnail asset placement: {path}")
        x, y, size = position["x"], position["y"], position["size"]
        if not (80 <= size <= 220 and 12 <= x <= 1268 - size and 12 <= y <= 708 - size):
            raise ValueError(f"Thumbnail asset is outside its canvas: {path}")
        if y < 420 and ((direction["subject_side"] == "left" and x < 610) or (direction["subject_side"] == "right" and x + size > 670)):
            raise ValueError(f"Thumbnail asset could obscure the reaction: {path}")
    return {**direction, "asset_positions": positions}
