# Architecture

## Flow

```text
VOD -> sparse frames -> stream states -> detected games -> reviewed games
                                                    |
                                  selected game -> transcript -> beats
                                                    |          |
                                              long plan    short candidates
                                                    |          |
                                             horizontal     vertical
```

The CLI stages map directly to future background jobs. JSON artifacts are the
contracts; the eventual website should consume them rather than contain media
logic.

## VOD Workspace

```text
workspace/<vod-id>/
  manifest.json            immutable source metadata
  frames/                  cached analysis frames
  stream-states.json       raw timestamp classifications
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
  short-choices.json       comparison of refined, audited stories; can select zero
  short-cuts.json          cached raw local boundary and contradiction reviews
  short-audits.json        cached raw whole-cut quality judgments
  short-diagnostics.json   selected/rejected counts and gate reasons
  frames/shorts/          cached denser frames around proposed scenes
  short-candidates.json    verified ranked stories with chronological source segments
  short-candidates.reviewed.json optional corrections matching raw version, game id, and signature
  short-v8-*.captions.reviewed.json optional corrected phrase and word timing per output
  short-v8-*.ass           generated active-word captions rendered by libass
  layout.json              normalized facecam crop
  layout.reviewed.json     optional human-corrected facecam, gameplay, and HUD crops
  reports/                 static review HTML
  renders/                 rough MP4 outputs
  usage.jsonl              model usage and estimated cost inputs
```

## Models

Game discovery, beat classification, and the current short-pipeline iteration
default to `gpt-5-nano`. Stronger short models are deferred until the pipeline
behavior is stable enough for a reviewed comparison. `whisper-1` supplies word
timestamps. Model names are configuration, never embedded in artifact logic.
Luna and Terra are later candidates for perception and global story planning.
`milloclips/league_glossary.json` is the shared League vocabulary source.
Per-VOD terms take priority when building Whisper's size-limited prompt.

## Editing Rules

- Horizontal output targets 9-12 minutes including its cold open and reusable cards, enforced in code as 540-720 seconds, and centers on one game.
- Code enforces duration; models rank and classify beats.
- Mechanical deletion targets silent travel, routine farming, shopping, and
  respawn time, but preserves meaningful dialogue and event setup.
- Open on meaningful speech or understandable action, preserve setup before a
  payoff, and cut dead tails after the reaction.
- Reviewed game boundaries may define `result_end`; long-form output must end
  on that Nexus/result beat rather than post-game review.
- Long-form selection keeps adjacent story units separate and never pads them to a
  footage target or unconditionally inserts pregame. A cached event-location pass
  uses 2s decoded samples with 20s surrounding context; it may expand once by 30s
  when setup/outcome is missing. A precise pass inspects +/-3s around EACH entry/exit
  at 0.25s spacing (one retry at +/-6s). Decisions include action, outcome, reaction
  and optional recall/shop/travel/wait anchors with frame/quote evidence.
  A scene may retain 1-6 chronological ranges to omit internal downtime.
  Start before action with necessary approach/premise; exit at recall onset or
  Arena between-round downtime after outcome and useful dialogue complete.
  Code protects words, checks continuing speech, and revalidates anchors, bounds,
  dense windows and non-overlap after adjustment. Invalid decisions require review,
  never silently restore the rough range. Reviewed corrections remain separate and
  bind to both input metadata and the fingerprint of the raw scene answer.
  Once cuts are approved, budgeting can request one cached global selection revision
  to add/remove optional complete story units, preserving required premise, ending,
  cold-open scene and retained boundaries. If still outside 540-720s it requires review.
  Model, boundary prompt version, source, beats and transcript fingerprints invalidate
  editorial caches without redownloading or retranscribing media.
- The actual final fight/result remains in the edit. An independently cached intro
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
  end. Shared source media and MP4 masters are cached under
  `assets/long/`, never in a VOD workspace.
- Champion select is eligible only when speech or chat establishes the premise;
  loading footage is never filler.
- Iteration 0 discovers shorts only within the selected game; whole-VOD discovery is deferred.
- Short discovery proposes events from all selected-game beats and runs a coarse
  visual scout across the whole selected game. A second pass uses transcript-health
  ranges to connect earlier spoken claims to later visible reversals. Per-game reviewed exclusions keep completed or
  rejected moments out of a fresh discovery run without changing raw evidence.
  Code checks spoken phrases against cached word timings and checks visual
  claims against sampled frame times; the model judges what the images depict.
  Hooks occur in the first two seconds and payoffs near the end.
  Accepted stories have one to four chronological scenes, target 20-45 seconds,
  and are hard-limited to 8-60 seconds. Every candidate is refined without changing
  its premise, then audited; Nano compares only those survivors. Exact quotes are located in cached
  word timings; the end waits for the following speech pause rather than cutting
  a sentence midway. A whole-cut audit rejects clips that only recount an unshown
  play or rants that merely repeat the same complaint. It can select zero, one, or
  two; rejected edits are never backfilled. A new discovery run does not silently
  reuse old reviewed story selections.
- A multi-scene short uses hard cuts and only self-evident chronological jumps.
  Captions contain dialogue only. Local transcription repairs can
  restore spoken captions where the game-length transcript was corrupted.
- Vertical output is 1080x1920 with a 680px facecam above 1240px gameplay.
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
`thumbnail-brief.json` contains three evidence-backed concepts; Nano produces a
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

FFmpeg seeks five seconds before each selected scene to decode reference frames,
then trims video and audio explicitly to the approved source interval. Boundary
sampling uses the same pre-roll and stores presentation times before resetting
output PTS. This prevents late starts on MPEG-TS VODs with non-zero input clocks.
It then handles deterministic cuts,
reusable-card animation, and vertical composition. Iteration 1 uses
ASS/libass for active-word captions without adding a rendering stack. Add
Remotion only when motion graphics need animation that ASS cannot express.
