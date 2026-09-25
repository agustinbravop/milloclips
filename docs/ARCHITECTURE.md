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
  vocabulary.json          optional per-VOD League names and terms (`{"terms": ["Rengar", "Fear"]}`)
  pregame-transcript.json  cached champion-select transcription when available
  beats.json               variable editorial units split near speech pauses
  long-plan.json           horizontal source intervals
  short-candidates.json    ranked short intervals
  short-candidates.reviewed.json optional human-selected exact boundaries
  short-captions.reviewed.json optional corrected phrase and word timing
  short.ass                generated active-word captions rendered by libass
  layout.json              normalized facecam crop
  layout.reviewed.json     optional human-corrected facecam and gameplay crops
  reports/                 static review HTML
  renders/                 rough MP4 outputs
  usage.jsonl              model usage and estimated cost inputs
```

## Models

All development roles default to `gpt-5-nano`. `whisper-1` supplies word
timestamps. Model names are configuration, never embedded in artifact logic.
Luna and Terra are later candidates for perception and global story planning.
`milloclips/league_glossary.json` is the shared League vocabulary source.
Per-VOD terms take priority when building Whisper's size-limited prompt.

## Editing Rules

- Horizontal output targets 9-12 minutes, enforced in code as 540-720 seconds, and centers on one game.
- Code enforces duration; models rank and classify beats.
- Mechanical deletion targets silent travel, routine farming, shopping, and
  respawn time, but preserves meaningful dialogue and event setup.
- Open on meaningful speech or understandable action, preserve setup before a
  payoff, and cut dead tails after the reaction.
- Reviewed game boundaries may define `result_end`; long-form output must end
  on that Nexus/result beat rather than post-game review.
- Duration balancing adds neighboring context and prefers trimming scene edges
  instead of punching holes into selected sequences.
- Long-form selections cover early, middle, and late game and avoid continuous
  runs longer than two minutes. Code reserves at least three minutes for each
  third of the game.
- Champion select is eligible only when speech or chat establishes the premise;
  loading footage is never filler.
- Iteration 0 discovers shorts only within the selected game; whole-VOD discovery is deferred.
- Short discovery sees every gameplay beat, selects contiguous beat ranges, scores
  hook/context/payoff/tightness separately, and removes overlapping lower-ranked candidates.
- Vertical output is 1080x1920 with a consistent 620px facecam above 1300px
  gameplay. Both panels scale to fill and crop overflow without changing aspect
  ratio; Spanish captions are centered over the seam between both panels.

## Rendering

FFmpeg handles deterministic cuts and vertical composition. Iteration 1 uses
ASS/libass for active-word captions without adding a rendering stack. Add
Remotion only when motion graphics need animation that ASS cannot express.
