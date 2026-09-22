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
  beats.json               classified 20-second editorial units
  long-plan.json           horizontal source intervals
  short-candidates.json    ranked short intervals
  layout.json              normalized facecam crop
  reports/                 static review HTML
  renders/                 rough MP4 outputs
  usage.jsonl              model usage and estimated cost inputs
```

## Models

All development roles default to `gpt-5-nano`. `whisper-1` supplies word
timestamps. Model names are configuration, never embedded in artifact logic.
Luna and Terra are later candidates for perception and global story planning.

## Editing Rules

- Horizontal output targets 10 minutes, uses a code-enforced 540–660 second tolerance, and centers on one game.
- Code enforces duration; models rank and classify beats.
- Mechanical deletion targets silent travel, routine farming, shopping, and
  respawn time, but preserves meaningful dialogue and event setup.
- Iteration 0 discovers shorts only within the selected game; whole-VOD discovery is deferred.
- Vertical output is 1080x1920 with facecam above gameplay and Spanish captions.

## Rendering

Iteration 0 uses FFmpeg for deterministic hard cuts and vertical composition.
Remotion is the preferred next layer for captions, zooms, replays, and reusable
visual treatments after the structural cut is proven.
