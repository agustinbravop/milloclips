# milloclips

AI-assisted editing pipeline for turning long Elmiillor Kick VODs into one
coherent League of Legends video and vertical shorts.

## Goal

Build the submission requested in [Elmiillor's AI editor challenge](https://x.com/Elmiillor/status/2100601890383835200)
using only eligible Kick VOD content from September 17 through October 1, 2026:

- one approximately 10-minute video edited entirely with AI;
- two vertical TikTok-style shorts edited entirely with AI;
- one AI-generated thumbnail for the long video;
- strong creativity, humor, and editorial judgment with substantially higher
  output quality and productivity than unaided editing.

The representative VOD for the current run is the September 23, 2026 League
of Legends stream, [ELISE BUFFS](https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e),
which is inside the challenge's permitted date range.

Iteration 0 deliberately uses cheap models and plain edits. It must produce:

- a static report of detected League games;
- a 9-12 minute horizontal rough cut from one selected game;
- a static report of short candidates;
- two 1080x1920 shorts with facecam above gameplay;
- one thumbnail for the long video.

## Setup

Requirements: Python 3.12+, `uv`, and an OpenAI API key for real analysis.
FFmpeg and yt-dlp are installed through Python dependencies.

```bash
uv sync
```

Place the key in the gitignored `.env` file: `OPENAI_API_KEY=...`.

## Commands

```bash
uv run milloclips vods --league-only
uv run milloclips ingest <kick-url-or-local-video>
uv run milloclips scan workspace/<vod-id>
uv run milloclips select workspace/<vod-id> game-001
uv run milloclips analyze workspace/<vod-id>
uv run milloclips plan-long workspace/<vod-id>
uv run milloclips render-long workspace/<vod-id>
uv run milloclips find-shorts workspace/<vod-id>
uv run milloclips render-short workspace/<vod-id>
```

Use `--mock` on AI commands to exercise the pipeline without API calls. Every
stage caches JSON artifacts under its VOD directory. Delete only the artifact
for the stage that should be rerun, or pass `--force`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for artifact contracts.

## Status

Iteration 0 is under active development. Automatic game ranking, polished
effects, and the website are intentionally deferred.
