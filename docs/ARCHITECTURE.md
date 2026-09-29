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
  long-plan.json           horizontal source intervals
  short-proposals.json     cached raw candidate stories from beat evidence
  short-visual-proposals.json raw visual leads from transcript-poor runs
  short-callback-proposals.json raw cross-game spoken-premise / visual-payoff leads
  short-visual-timing.json cached exact frame checks for fleeting payoff banners
  short-exclusions.reviewed.json optional per-game ranges to skip on a fresh discovery run
  short-verdicts.json      cached raw frame/dialogue judgments per proposal
  short-choices.json       comparison of verified stories; can select zero
  short-cuts.json          cached raw local boundary and contradiction reviews
  short-audits.json        cached raw whole-cut quality judgments
  frames/shorts/          cached denser frames around proposed scenes
  short-candidates.json    verified ranked stories with chronological source segments
  short-candidates.reviewed.json optional corrections matching raw version and game id
  short-captions.reviewed.json optional corrected phrase and word timing
  short.ass                generated active-word captions rendered by libass
  layout.json              normalized facecam crop
  layout.reviewed.json     optional human-corrected facecam, gameplay, and HUD crops
  reports/                 static review HTML
  renders/                 rough MP4 outputs
  usage.jsonl              model usage and estimated cost inputs
```

## Models

Game discovery and beat classification default to `gpt-5-nano`. Short
editorial discovery, verification and comparison use `gpt-5-mini` after a reviewed
head-to-head on the September 23 game, where Nano picked a routine Flash
trade and Mini picked the more compelling Graves reversal. `whisper-1`
supplies word timestamps. Model names are configuration, never embedded in artifact logic.
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
- Short discovery proposes only exceptional events from all selected-game beats,
  then examines cached frames around each proposed payoff before ranking anything.
  A separate scout inspects cached gameplay frames when transcription degenerates
  into repeated tokens; the editorial model can connect earlier spoken claims
  to later visible reversals. Per-game reviewed exclusions keep completed or
  rejected moments out of a fresh discovery run without changing raw evidence.
  Code checks spoken phrases against cached word timings and checks visual
  claims against sampled frame times; the model judges what the images depict.
  Hooks occur in the first two seconds and payoffs near the end.
  Accepted stories have one scene or 2-7 chronological scenes, total 8-90 seconds.
  Mini compares evidenced stories, then a local cut review can open on a stronger
  prediction and end on its later correction. Exact quotes are located in cached
  word timings; the end waits for the following speech pause rather than cutting
  a sentence midway. A whole-cut audit rejects clips
  that only recount an unshown play. It can select zero; rejected edits do not
  renumber surviving render filenames. A new discovery version does not silently
  reuse old reviewed story selections.
- A multi-scene short uses hard cuts. When a source jump is at least a minute,
  a brief blue, centered editor label uses its own font while word captions
  retain their normal style at the panel seam. Local transcription repairs can
  restore spoken captions where the game-length transcript was corrupted.
- Vertical output is 1080x1920 with a 680px facecam above 1240px gameplay.
  Both panels scale uniformly to fill and crop overflow without distortion;
  captions are timed against the concatenated scenes at the panel seam. A third
  crop copies the original League KDA, CS, and game-time HUD from the same
  source frames into the gameplay panel's top right, below the speech captions.

## Rendering

FFmpeg handles deterministic cuts and vertical composition. Iteration 1 uses
ASS/libass for active-word captions without adding a rendering stack. Add
Remotion only when motion graphics need animation that ASS cannot express.
