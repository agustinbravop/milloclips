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
uv run milloclips auto <kick-url-local-video-or-existing-vod-workspace>
uv run milloclips scan workspace/<vod-id>
uv run milloclips select workspace/<vod-id> game-001
uv run milloclips analyze workspace/<vod-id>
uv run milloclips plan-long workspace/<vod-id>
uv run milloclips render-long workspace/<vod-id>
uv run milloclips review-long workspace/<vod-id>
uv run milloclips find-shorts workspace/<vod-id>
uv run milloclips render-shorts workspace/<vod-id>
uv run milloclips thumbnails workspace/f1ca1ecfc69f
```

Use `--mock` on AI commands to exercise the pipeline without API calls. Every
stage caches JSON artifacts under its VOD directory. Delete only the artifact
for the stage that should be rerun, or pass `--force`.

`auto` ingests or resumes a VOD, scans it, analyzes every detected game in an
isolated `game-analyses/` cache, ranks the games editorially, and renders the
long video, passing shorts, and thumbnail for the winner. It writes
`game-ranking.json` and `auto-summary.json` in the VOD workspace. Use
`--force` to refresh cached analysis stages.

`plan-long` selects complete story scenes and caches its raw local boundary
decisions in `long-cuts.json`; optional corrections belong in
`long-cuts.reviewed.json`. `render-long` adds a punchy verified cold open of up to
20 seconds, a 2-second animated `ElmilloR` website-icon bumper, and a 12-second
Fiddlesticks trailer end card after the final result. Reusable MP4 masters and
their source media live under `assets/long/`; per-video `renders/` contains only
rendered video outputs.
`review-long` renders four seconds around every join into
`renders/long-transitions.mp4` and records labeled output times in
`long-review.json` for fast transition QA.

`thumbnails` creates three evidence-backed title/thumbnail concepts from the
selected final edit, ranks them, and writes the winner to `renders/thumbnail.png`.
The contact sheet, candidate scores, and cached `thumbnail-brief.json` remain
under `renders/thumbnails/` for optional review. No AI-generated Rift
backgrounds are used. Add streamer reaction cutouts to `assets/faces/` (see its
README); no stream facecam crop is used. Official Riot icons are optional and
must be relevant to the evidenced hook.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for artifact contracts.

## Website

The public gallery lives in [`website/`](website/) and is a separate SvelteKit
app reading `static/catalog.json`. See [`website/README.md`](website/README.md).
Automatic game ranking and polished effects are still deferred.

## Status

Iteration 0 is under active development.
