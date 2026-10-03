# milloclips website

Public gallery of League of Legends streams and the edits the pipeline cut from
them: a static rundown where published streams show a long-form edit plus its
shorts side by side, and queued streams say why they are still waiting.

All visitor-facing copy is Spanish (Spain); the code is English.

## Run it

```bash
bun install
bun run dev
```

## Verify

```bash
bun run test    # catalog contract
bun run check   # svelte-check
bun run build
```

## Content model

`static/catalog.json` is the only input. A stream is **published** when it has at
least one clip, otherwise it renders greyed out with its `pendingReason` as the
reason. Reasons are keys of `PENDING_REASONS` in `src/lib/types.ts`, which owns
the Spanish wording, so the data never repeats the same sentence per stream.

```json
{
  "id": "809acd6b-...",
  "title": "ELISE BUFFS | NO ME CORTO LA BARBA HASTA C1",
  "date": "2026-09-23",
  "duration": 14771,
  "kickUrl": "https://kick.com/elmiillor/videos/809acd6b-...",
  "thumbnail": "/images/vod-809acd6b.png",
  "pendingReason": "not_ingested",
  "long": {
    "id": "809acd6b-long",
    "title": "Elise Buffs: la partida que se decide con el Baron",
    "duration": 616,
    "width": 1280,
    "height": 720,
    "video": { "provider": "file", "src": "/media/sample-long.mp4" }
  },
  "shorts": []
}
```

`video.provider` is `file` (public URL to an MP4) or `youtube` (YouTube video
id). Switching a published video to YouTube is a one-field edit.

## Publishing

The gallery is written by `uv run milloclips publish <vod_dir> --approve` from
the repository root; there is no admin panel or upload flow. It reads the VOD's
existing artifacts, matches the catalog entry by the Kick URL in `manifest.json`
(override with `--vod-id`), upserts the `long`/`shorts` records, and stages
assets under `static/`. Media is hardlinked, not copied. The clip title comes
from the thumbnail process, the poster from `renders/thumbnail.png`. Pass
`--youtube <video_id>` to record the long form as `provider: youtube` once it is
uploaded, leaving every other field unchanged.

## What is real and what is provisional

- Streams, titles, dates, durations, Kick URLs: real Kick data.
- `images/vod-809acd6b.png`: the real generated thumbnail.
- Clip `duration`/`width`/`height`: measured from the actual pipeline renders
  (`long-rough.mp4`, `short-01.mp4`, `short-02.mp4`).
- `static/media/*.mp4`: synthetic ffmpeg placeholders, so playback is testable
  before real MP4s are published. Replace them; no code changes needed.
- Provisional copy: the long-form titles were written by hand for the prototype
  (derived from each `long-plan.json` story), and `809acd6b-short-02`
  ("El Baron que nadie vio venir") is a layout placeholder — the pipeline has
  only produced one real short for that stream so far. `f50debc3` shows a
  published long edit with no shorts because its only short render is stale
  (no validation sidecar), so it is not publishable yet.

## Not built yet

No admin panel, database, or upload flow. `publish` writes the same catalog
records directly, so the rundown stays untouched.
