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
`long-cuts.reviewed.json`. The boundary editor locates event setup, action,
outcome/reaction and downtime, then inspects each entry/exit at quarter-second
spacing. It can remove internal recall/shop/return-travel gaps; useful dialogue
is retained through completion. Missing setup/outcomes trigger a bounded context
expansion; unresolved or invalid decisions are reported in
`long-boundary-review.json` instead of restoring rough footage. Frame manifests
under `frames/long-boundaries/` store decoded source-relative timestamps.
Duration balancing preserves approved cuts and required premise/ending scenes;
one cached editorial selection revision may add/remove optional complete moments.
It never pads travel to reach the duration target. Editorial cache keys include
model, prompt version, source and transcript inputs; media/transcription remain cached.
`plan-long` separately chooses a relevant champion-select/early-game setup intro
or a visibly successful cool play by ElmilloR. Failed plays, deaths and teammate
highlights cannot be cold opens. Setup intros replace pre-action preparation;
the main edit resumes before the first fight without repeating setup or showing
loading. `long-intro.json` caches raw choices and verification. If a concise intro
makes the edit too short, `long-intro-budget.json` adds a complete main-video event
while reusing approved cuts; intro silence is never padded to meet the duration.
`render-long` adds a 5-second icon-only card with centered glow and a full circular
music visualizer, then a 12-second
Fiddlesticks trailer end card after the final result. Reusable MP4 masters and
their source media live under `assets/long/`; per-video `renders/` contains only
rendered video outputs.
Gameplay dissolves into the icon card over 0.5 seconds. Music starts one second
before the dissolve, fades in quadratically over two seconds (full level one
second into the card), and fades out over one second. The title-card master is
versioned through `assets/long/title-card.json` so animation changes regenerate it.
`review-long` renders eight seconds around every join into
`renders/long-transitions.mp4` and records labeled output times in
`long-review.json`, with outgoing/incoming source times and cut reasons for QA.

Existing v15 plans upgrade through `plan-long` to the v16 intro contract without
repeating scene analysis; older v14 plans also require boundary regeneration.
Reviewed decisions use `cuts_version: 2`, the same input
metadata as `long-cuts.json`, and entries shaped as
`{"raw_signature": "<fingerprint of raw scene answer>", "decision": {"segments": [...]}}`.
The decision carries the same event anchors, evidence, context flags and optional
cold-open fields as a raw precise decision; stale overrides fail explicitly.

`thumbnails` creates three evidence-backed concepts, then Nano art-directs each
using the channel's approved visual references. **GPT Image 2.5 Sunburst** edits a
masked placement template into game-faithful scene artwork, effects, and headline
typography. Original reaction-photo and icon layers are restored after generation
and final resizing: the model cannot replace ElmilloR's real face or redraw exact
icons in the final image. Six-item layouts are available when actual item IDs are
evidenced; there is no mandatory champion badge or fixed title layout.

The four reactions in `assets/faces/` are reused; `framing.json` defines normalized
head-focused source crops. Reference video URLs and the user's visual notes live
in `assets/thumbnails/references.json`. `--prepare-references` caches their actual
YouTube thumbnails without AI calls. Source images are style examples, never
permission to invent the same champions, stats or creator faces in another video.

The contact sheet, **280x158 feed previews**, candidate metadata, source placement
templates/masks, raw Sunburst artwork, request/usage records, and Nano's visual
review remain under `renders/thumbnails/`. `thumbnail-brief.json` caches concepts
in the VOD workspace. Candidate 1 is a provisional `renders/thumbnail.png` until
a winner is explicitly selected:

```bash
uv run milloclips thumbnails workspace/f1ca1ecfc69f --choose 2
uv run milloclips thumbnails workspace/f1ca1ecfc69f --choose-ai
uv run milloclips thumbnails workspace/f1ca1ecfc69f --revise 1 --headline "BARÓN AL LÍMITE" --edit "Correct the headline; keep the original face and scene."
```

Selection does not regenerate candidates. Generation writes `review.raw.json`
with a recommendation and publication judgment, comparing full-size and feed-size
renders. `--choose-ai` can adopt that review; its decision is cached separately in
`selection.raw.json`. `--revise` makes one explicit image-edit call, retaining the
original artwork. Corrections live in `revisions.reviewed.json`; matching rerenders
retain them, while changed source renders invalidate those overrides.
Human picks live separately in `selection.reviewed.json`. `selection.json`
records the effective choice; unchanged rerenders preserve the selection.
`--force` refreshes concepts without redownloading cached League assets.
Concepts receive a separate evidence/copy audit; real runs stop for review if
fewer than three pass instead of silently backfilling generic thumbnails.

Each Sunburst request is cached by prompt, model settings, mask and all input image
contents. `--force` refreshes concepts, not unchanged paid artwork. The default
`--image-budget 5` is a cumulative image spend/reservation limit for the workspace:
`image-usage.json` records actual token-based costs when reported, or a conservative
$1.20 reservation for an unmetered/unknown call. Admission is estimate-based, not a
provider-side hard billing cap; Nano editorial calls are logged separately. SDK
image retries are disabled to avoid duplicating potentially billable timed-out
generations. `--mock` uses the deterministic fixture renderer without downloads or
image API calls.

Channel-wide style and reusable principles live in
[`assets/thumbnails/preferences.md`](assets/thumbnails/preferences.md). Specific
candidate judgments live in each VOD's `thumbnail-feedback.reviewed.md` and are
read only for that workspace. Edit either file, tell the coding agent your
preferences, or append a scoped note:

```bash
uv run milloclips thumbnails workspace/f1ca1ecfc69f --feedback "Use support, not apoyo. Prefer a concrete funny mistake over generic advice."
uv run milloclips thumbnails workspace/f1ca1ecfc69f --feedback-scope video --feedback "The previous third concept has a weak hook; replace it."
uv run milloclips thumbnails workspace/f1ca1ecfc69f
```

Saving feedback does not render or call AI. On the next generation, preferences
and this video's review notes are supplied to the concept writer, evidence/copy
auditor, art director, image generator, and image selector. Candidate numbers have
no fixed editorial roles; each concept chooses its visual treatment from its hook.
Changes invalidate thumbnail concept and AI-choice caches while retaining
downloaded media, transcripts, frames, and League assets. AI choice includes a
`publishable` judgment and `next_changes`, so best-of-three need not mean ready
to publish. References guide the style, while reviewed notes can change generated
scenes, typography and effects without editing fixed templates. Exact foreground
asset handling and validation remain deterministic code.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for artifact contracts.

## Website

The public gallery lives in [`website/`](website/) and is a separate SvelteKit
app reading `static/catalog.json`. See [`website/README.md`](website/README.md).
Automatic game ranking and polished effects are still deferred.

## Status

Iteration 0 is under active development.
