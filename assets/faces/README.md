# Streamer reaction photos

The concept writer sees the actual available reaction photos and the emotional
descriptions in `reactions.json`. It must explain each selection in
`reaction_reason`, matching evidence, headline and emotional promise rather than
using a fixed hook-to-expression mapping. PNG, JPG, and WebP work;
transparent PNG cutouts produce the best white-rimmed foreground silhouette.
The current four cutouts are reused across VODs. Real runs require an available
photo. Add a named cutout and catalog entry for a new expression; its selection
becomes available automatically. Photo contents/descriptions invalidate concept
caches. Right placement mirrors the photo inward; left placement stays unflipped.
Revision records retain their original mirroring, so edits do not alter old poses.

Re-run `uv run milloclips thumbnails
workspace/f1ca1ecfc69f` to composite photos using cached concepts and assets.
More poses per expression can be added when the concept selector needs them;
currently the renderer uses one named image per expression.

`framing.json` contains normalized `[left, top, right, bottom]` head-focused
source crops for Sunburst placement templates. Adjust these when adding or
replacing a photo. The exact cropped/outlined image is restored after generation;
no AI face or pose is used in the final thumbnail.

Prefer `smiling` for most positive/playful concepts and normal excitement.
`overly-hyped` is reserved for rare, genuinely extreme emotional peaks, with
specific evidence explaining why a smile would undersell the moment. The former
`hyped.png` is now `overly-hyped.png`; old revision paths remain readable.
