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
  long-cuts.json           raw local AI boundary decisions for long-form scenes
  long-cuts.reviewed.json  optional reviewed boundary overrides keyed by beat range
  long-plan.json           refined scenes, cold open, and reusable-card durations
  long-review.json         labeled output times for every long-form transition
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
- Long-form planning selects complete chronological scenes, then a second cached
  pass uses dense local frames and timestamped words to start before setup/action
  and end after the outcome/reaction. Reviewed corrections remain separate from
  raw model answers. Duration balancing keeps contiguous context instead of
  punching holes into scenes; it does not force filler from each third.
- The actual final fight/result remains in the edit. The strongest verified payoff
  is duplicated as a cold open for its complete natural duration, capped at 20 seconds.
- A reusable 2-second animated `ElmilloR` bumper reveals the channel website icon
  with a camera-flare sweep. A reusable 12-second Fiddlesticks trailer end card
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

FFmpeg seeks each selected scene independently, then handles deterministic cuts,
reusable-card animation, and vertical composition. Iteration 1 uses
ASS/libass for active-word captions without adding a rendering stack. Add
Remotion only when motion graphics need animation that ASS cannot express.
