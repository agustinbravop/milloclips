# Architecture

## Flow

```text
VOD -> scan -> detected games -> analyze every game -> rank -> long plan -> long edit
 |                                                                  -> thumbnail
 +-> game evidence + opening/between-game/closing evidence -> judge -> two shorts
                                                                    |
                                            review -> YouTube upload -> Unlisted check
                                                                    -> gallery link
```

JSON artifacts connect the stages. `auto` generates outputs; publishing is a
separate operator action. The SvelteKit gallery reads `website/static/catalog.json`.
For command usage, run `uv run milloclips COMMAND --help`.

## Workspaces

```text
workspace/<vod-id>/
  manifest.json            source metadata and local media path
  stream-states.json       scan classifications
  games.json               detected game boundaries
  games.reviewed.json      optional corrected boundaries
  selection.json           long-form winner
  game-ranking.json        cached all-game comparison
  status.json              auto stage, success/error, source workspace
  auto-summary.json        output paths; workspace field identifies the edit directory
  renders/                 canonical final videos and thumbnail for this stream
  edit-input.json          selected analysis cache and its input fingerprints
  game-analyses/<game-id>/  isolated internal game analysis caches
  vod-shorts/              whole-VOD short evidence, judgment, and cut decisions
```

`rank-games` caches each game's analysis, ranks all games, selects the winner, and
prints its internal analysis workspace. `auto` promotes selected inputs to the
stream workspace and writes all deliverables into `workspace/<vod-id>/renders/`.
Uncovered source footage is transcribed in ten-minute regions without a redundant
visual beat-classification pass; evidence coverage remains continuous.

### Edit-stage artifacts

Standalone selected-game stages can also write these into the original VOD
workspace. In an `auto` run, selected inputs and final plans live at stream level;
whole-VOD short decisions live in `vod-shorts/`, linked from the edit directory.

```text
<edit-workspace>/
  manifest.json            immutable source metadata
  frames/                  cached analysis frames
  games.json               detected boundaries
  games.reviewed.json      optional human corrections
  selection.json           selected game id
  transcript.json          selected-game timestamped words
  transcript.reviewed.json optional exclusion ranges and timestamp-preserving word replacements
  short-transcript.json      cached local Whisper retry for corrupt selected-game audio
  short-transcript.reviewed.json optional corrections to the local retry
  vocabulary.json          optional per-VOD League names and terms (`{"terms": ["Rengar", "Fear"]}`)
  pregame-transcript.json  cached champion-select transcription when available
  beats.json               variable editorial units split near speech pauses
  long-proposals.json      raw global long-form scene selection
  long-cuts.json           v2 raw event-location and precise-boundary decisions keyed by beat range
  long-cuts.reviewed.json  optional input-bound overrides with per-scene raw_signature/decision
  long-boundary-review.json ready/needs_review status with failing scene and reason
  frames/long-boundaries/*/frames.json decoded source-relative frame times and sampling identity
  long-intro.json          raw setup/successful-play proposal and frame/quote verification
  long-intro-budget.json   optional raw selection of extra main-video events after a concise intro
  long-plan.json           v16 intro, retained story/body ranges, anchors and dissolve-aware durations
  long-review.json         labeled joins with source times, entry/exit reasons and 8s review windows
  short-proposals.json     cached raw candidate stories from beat evidence
  short-visual-proposals.json raw visual leads from transcript-poor runs
  short-callback-proposals.json raw selected-game spoken-premise / visual-payoff leads
  short-visual-timing.json cached exact frame checks for fleeting payoff banners
  short-exclusions.reviewed.json optional per-game ranges to skip on a fresh discovery run
  short-verdicts.json      cached raw frame/dialogue judgments per proposal
  short-choices.json       source-moment judge ranking, best pair then alternatives
  short-cuts.json          cached raw shorts-specific cuts for ranked picks
  short-event-cuts.json    cached shared event-location and dense boundary answers
  short-event-boundary-review.json event completion and boundary construction status
  short-editing-status.json construction/repair status for the preserved selection
  short-diagnostics.json   selected/rejected counts and gate reasons
  frames/shorts/          cached denser frames around proposed scenes
  short-candidates.json    verified ranked stories with chronological source segments
  short-candidates.reviewed.json optional corrections matching raw version, game id, and signature
  short-*.captions.reviewed.json optional corrected phrase and word timing per output
  short-*.captions.raw.json cached raw punctuation/case formatting, separate from corrections
  short-*.ass              generated active-word captions rendered by libass
  layout.json              normalized facecam crop
  layout.reviewed.json     optional human-corrected facecam, gameplay, and HUD crops
  reports/                 static review HTML
  renders/                 rough MP4 outputs
  usage.jsonl              model usage and estimated cost inputs
  youtube/<clip>.json      upload identity, resumable session, video URL, confirmation
  youtube/long-thumbnail.jpg uploaded long-video poster
```

## Models

Game discovery, beat classification, and the current short-pipeline iteration
default to `gpt-5-nano`. Stronger short models are deferred until the pipeline
behavior is stable enough for a reviewed comparison. `whisper-1` supplies word
timestamps. Model names are configuration, never embedded in artifact logic.
`milloclips/league_glossary.json` is the shared League vocabulary source.
Per-VOD terms take priority when building Whisper's size-limited prompt.

Text/vision requests allow at most 32 images, with event anchors retained when
subsampling. Exhausted credit is never retried; transient rate limits allow one
retry. `requests/<content-hash>.json` reuses identical raw responses, and
`usage.jsonl` records request identity, timestamps, image counts and token usage.
The separate thumbnail image-reservation budget remains in force.

## Cache and reviewed corrections

Matching stage artifacts and validated render sidecars are reused. Interrupted
beat classifications are saved per completed batch. Editorial changes do not
redownload the source or invalidate matching transcripts. `--force` is stage-specific:
scan resamples frames; analysis refreshes classifications; planning refreshes
editorial choices; thumbnails refreshes concepts while reusing unchanged paid artwork.

Raw model answers and reviewed corrections remain separate. Long-cut corrections
use `cuts_version: 2`, the raw file's input metadata, and entries shaped as:

```json
{"raw_signature": "<fingerprint of raw scene answer>", "decision": {"segments": []}}
```

The full decision also carries the original anchors, evidence, context flags, and
optional cold-open fields. Stale overrides fail explicitly. Existing v15 plans
upgrade through `plan-long` to the v16 intro contract; v14 plans also require
boundary regeneration. Boundary policy v3 preserves media and game transcripts.

## Editing Rules

- Horizontal output prefers 10-12 minutes when meaningful content supports it,
  and centers on the most entertaining available game. It targets 540-720 seconds
  including cards but does not discard a shorter, source-bounded story to satisfy
  an entertainment floor. Repeated complaints are not preferred over humor or play.
- Code enforces duration; models rank and classify beats.
- Mechanical deletion targets silent travel, routine farming, shopping, and
  respawn time, but preserves meaningful dialogue and event setup.
- Open on meaningful speech or understandable action, preserve setup before a
  payoff, and cut dead tails after the reaction.
- Round-based edits preserve early rounds in order and meaningful build progression.
  Item/augment analysis and selection, plus relevant host/map/mechanic changes,
  are useful breathers rather than routine shopping. Tiny fragments must have a
  complete payoff or consequential follow-up. The final scene shows the resolved
  result/placement, completes dialogue, and retains at least one speech-free second;
  final-result breathing space is exempt from routine downtime trimming.
  If no reviewed `result_end` exists, final-scene inspection may use up to 15 seconds
  beyond scanner gameplay end, capped by available source bounds. Scanner postgame
  boundaries do not shorten this inspection window. Only
  this tail is transcribed into `long-ending-transcript.json`; the game transcript
  remains cached. Exit at the first natural pause after the immediate result reaction,
  rather than including renewed post-game review or next-match talk.
- Reviewed game boundaries may define `result_end`; long-form output must end
  on that Nexus/result beat rather than post-game review.
- Long-form selection keeps adjacent story units separate and never pads them to a
  footage target or unconditionally inserts pregame. A cached event-location pass
  uses a bounded overview of decoded samples with surrounding context; it may
  expand once when setup/outcome is missing. Word-safe arithmetic replaces the
  default dense paid re-review. Decisions include action, outcome, reaction
  and optional recall/shop/travel/wait anchors with frame/quote evidence.
  A scene may retain 1-6 chronological ranges to omit internal downtime.
  Start before action with necessary approach/premise; exit at recall onset or
  Arena between-round downtime after outcome and useful dialogue complete.
  Code protects words, checks continuing speech, and revalidates anchors, bounds,
  source bounds and non-overlap after adjustment. An unresolved review retains the
  selected source window with an explicit boundary note instead of discarding the
  stream. Reviewed corrections remain separate and
  bind to both input metadata and the fingerprint of the raw scene answer.
  Once cuts are approved, budgeting can request one cached global selection revision
  to add/remove optional complete story units, preserving required premise, ending,
  cold-open scene and retained boundaries. Thin material yields its best available
  shorter edit; the 720-second upper bound remains enforced.
  Model, boundary prompt version, source, beats and transcript fingerprints invalidate
  editorial caches without redownloading or retranscribing media.
- The actual final fight/result remains in the edit. The default introduction is
  the complete opening premise from the approved story, retained once and split
  at a source-word boundary. This avoids separate intro proposal, verification
  and duration-rebuild calls. Historical optional intro artifacts support a cached intro
  pass chooses relevant champion-select/early-game setup (8-40s, up to four cuts)
  or a complete successful cool play by ElmilloR (5-20s, one cut). Cold opens require
  his own visible success: deaths, blunders, failed predictions and teammate-only
  highlights are disallowed. An unsuccessful verification retries once as setup.
  Setup replaces pre-action preparation and the body resumes before the first fight;
  cold opens remain duplicated in their chronological scene. Short intros are not
  padded: if needed, one additional cached selection adds a complete main-video event,
  refining only new units and preserving all approved ranges. `story_segments`
  preserves the unabridged scene selection; `segments` is the post-intro body.
  `intro` contains kind/reason/evidence and source ranges. `cold_open` aliases it
  for the renderer contract; intro length is the sum of ranges, not their outer span.
- A reusable 5-second icon-only card gently fades in a centered website icon,
  glow and full circular audio-reactive ring, without text, recoil or flash.
  A 0.5s gameplay-to-card dissolve overlaps the final intro footage with the card;
  all duration, music and transition arithmetic subtracts that overlap. Intro speech
  is retained through its end. Music starts 1s before card appearance, takes 2s to
  reach its reduced gain, and fades out over 1s using quadratic curves. The master
  caches its version, icon and music identity in `assets/long/title-card.json`.
  A reusable 12-second Fiddlesticks trailer end card
  carries subscribe, Kick, and X messaging through 0:10, then fades through the
  end. End-card music gain is 0.103125, with a one-second lead and linear two-second
  fade-in reaching full configured gain one second after card appearance.
  Shared source media and MP4 masters are cached under
  `assets/long/`, never in a VOD workspace.
- Champion select is eligible only when speech or chat establishes the premise;
  loading footage is never filler.
- Shorts discovery covers the entire VOD independently of the long-form game.
  `pipeline.py` reuses cached game evidence and analyzes uncovered opening,
  between-game and closing footage in bounded regions. `vod-shorts/` merges
  beats, words and frame timestamps onto an absolute VOD clock before discovery
  and the single source-moment judge. Full source coverage is recorded explicitly.
  Winner workspaces link to this selection; render validation checks its signature
   and source identity. Long-video event boundaries are reused on that same clock.
- Current short eligibility is League of Legends footage (including draft/queue)
  with meaningful streamer speech. Slots/other games, silent gameplay and game
  announcer lines are excluded before judgment. Verified speech must match a complete
  non-corrupt transcript phrase, and construction must preserve that phrase.
- Short discovery uses one text-led path over the entire VOD, with the cheaper
  analysis model. Cached overlapping windows each nominate their best complete
  stories; at most six reach factual review. There is no extra global callback or
  full-VOD visual-scout pass. Multi-scene callbacks remain eligible through the
  same discovery path. Bounded candidate images establish what actually happened.
  Meaningful streamer speech, complete setup, actual outcome and spoken reaction
  are required; general remarks are not automatically punchlines.
  A single editorial comparison ranks the best available supported pair. Empty
  entertainment recommendations do not discard otherwise supported stories.
  The verifier already provides complete cuts: there is no second paid re-edit.
  Code retains the entire payoff phrase (`payoff_end`), not just its starting
  anchor, and adds about three seconds for reaction, finishing active speech.
  One to four chronological segments must fit **8-60 seconds total** including
  all setup, payoff and reaction; a longer complete story is skipped, not truncated.
  Source bounds, non-overlap, speech evidence and full payoff retention remain
  hard invariants. Quality is relative to the source, not a minimum score gate.
  Whole-VOD evidence and raw reviews remain cached separately from corrections.
- A multi-scene short uses hard cuts and only self-evident chronological jumps.
  Captions contain dialogue only. Spanish speech may contain English phrases/gaming terms;
  transcription and caption formatting preserve each word's spoken language, never translate.
  Caption chunking normally uses cached source words locally. An existing optional
  punctuation correction is aligned to
  source words with original timings; inserted, removed or translated model words never replace
  the source dialogue. Known inverted-mark spans receive closing punctuation, and this style
   omits opening `¡`/`¿`. Reviewed caption corrections remain separate and take precedence.
   Caption rendering removes `!`, `¡` and `.` while retaining question marks;
   raw punctuation/case decisions and word timing stay intact.
  Local transcription repairs can
  restore spoken captions where the game-length transcript was corrupted.
- Vertical output is 1080x1920 with a 680px facecam above 1240px gameplay.
  The default gameplay crop is normalized `[0.22, 0, 0.49, 1]`, biased left to
  retain the champion-stats HUD. Per-VOD explicit crops can override it. Render
  layouts for whole-VOD shorts come from each candidate's source-region analysis,
  including regional reviewed crops, rather than the first game's layout. Render
  provenance fingerprints the effective layout, including defaults, so a framing
  change invalidates older renders.
  Both panels scale uniformly to fill and crop overflow without distortion;
  captions are timed against the concatenated scenes at the panel seam. A third
  crop copies the original League KDA, CS, and game-time HUD from the same
  source frames into the gameplay panel's top right, below the speech captions.
  Short audio is loudness-normalized. Each MP4 has a sidecar binding it to the
  candidate, captions, layout, and source; code validates duration, dimensions,
  and audio after rendering.

## Rendering

### Thumbnail artwork

The thumbnail stage uses approved YouTube examples from
`assets/thumbnails/references.json` as a cached visual reference library.
`thumbnail-brief.json` contains one evidence-backed concept; the configured model produces a
cached narrative art direction and exact foreground-asset placement per concept.
`gpt-image-2.5-sunburst` edits a photo/icon placement template at 1536x864 into a
finished image in one call per candidate. Style-reference photos stay with Nano;
the image generator receives only ElmilloR's photo as its human identity reference.
Pillow resizes to 1280x720 without
pasting the photo over text; identity and icon fidelity are reviewed, not guaranteed
pixel-exact. Right-side photos remain unflipped; left-side photos are mirrored. Normalized
head crops live in `assets/faces/framing.json`.

Paid image requests are content-addressed by settings, prompt, reference bytes
and mask. Raw PNGs, request JSON, usage records, layout/mask PNGs and direction
answers stay under `renders/thumbnails/`. `image-usage.json` tracks actual costs
or conservative reservations, and image SDK retries are disabled. Mock fixtures
retain the deterministic renderer without image calls or reference downloads.

The contact sheet and 280x158 feed previews remain under `renders/thumbnails/`.
The default cumulative image budget is $5 per workspace, using actual reported
costs or a conservative $1.20 reservation per unknown/unmetered request. This is
an admission estimate, not a provider billing cap; Nano calls are logged separately.

Channel-wide style lives in `assets/thumbnails/preferences.md`; per-video judgments
live in `thumbnail-feedback.reviewed.md`. Feedback changes concept/art-direction
inputs, not cached media. Four reusable reaction photos use `assets/faces/framing.json`.
Source faces point left: right placement is unflipped; left placement is mirrored.

Generation renders two candidates, exports candidate 1 and skips the automated
visual review. The human compares the contact sheet and selects explicitly, so
human picks in `selection.reviewed.json` survive unchanged rerenders. `--choose-ai`
still runs the Nano full-size/feed-size comparison on demand and caches the raw
judgment in `selection.raw.json` with conservatively validated publication checks.
`--revise` edits one candidate without style-reference images (which can leak
another creator's face). Baked-photo candidates retain the finished model image;
legacy pixel-locked candidates retain their original restoration/orientation. Corrections and resulting versioned renders
are recorded separately in `revisions.reviewed.json`. Matching base-image
digests preserve revisions across rerenders; changed bases invalidate them.

`selection.json` is the effective thumbnail choice. Candidate 1 is provisional
until chosen; local picks live in `selection.reviewed.json`. Choosing a candidate
does not regenerate images. Concept evidence/copy audits stop real runs if fewer
than two concepts pass. Optional AI selection records `publishable` and
`next_changes`; winning a comparison does not imply the artwork is ready to publish.

FFmpeg seeks five seconds before each selected scene to decode reference frames,
then trims video and audio explicitly to the approved source interval. Boundary
sampling uses the same pre-roll and stores presentation times before resetting
output PTS. This prevents late starts on MPEG-TS VODs with non-zero input clocks.
It then handles deterministic cuts,
reusable-card animation, and vertical composition. Iteration 1 uses
ASS/libass for active-word captions without adding a rendering stack. Add
Remotion only when motion graphics need animation that ASS cannot express.

## Gallery publishing

`catalog-sync` adds missing League streams by Kick URL without changing existing
entries. Local `publish` validates renders, stages media as hardlinks (copy fallback),
copies the thumbnail and short poster frames, and replaces that VOD's clip records.
It uses the chosen/provisional thumbnail title for long form and candidate titles
for shorts. `--youtube ID` substitutes an existing long-video embed without a
network visibility check.

Individual YouTube uploads save progress but leave the gallery untouched.
`publish --confirm-youtube` verifies the saved upload's channel, Unlisted status,
completed processing, and embedding before updating one clip and its poster;
sibling clips are preserved. Gallery changes are local until the website is deployed.
Account setup and recovery are documented in [YOUTUBE.md](YOUTUBE.md).
