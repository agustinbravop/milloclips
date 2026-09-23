# milloclips

AI-assisted editing pipeline for turning long Elmiillor Kick VODs into one
coherent League of Legends video and vertical shorts.

Iteration 0 deliberately uses cheap models and plain edits. It must produce:

- a static report of detected League games;
- a 9-12 minute horizontal rough cut from one selected game;
- a static report of short candidates;
- one 1080x1920 short with facecam above gameplay.

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
