# milloclips

Turn Elmiillor Kick VODs into a ~10-minute League of Legends edit, two vertical
shorts, and an AI-generated thumbnail. Rank every detected game for the long
edit; discover shorts independently across the whole VOD, including between games.
For now, shorts are League of Legends moments with meaningful Elmiillor speech;
slots/other games and silent gameplay are excluded. Complete stories may run up to 90 seconds.

For [Elmiillor's AI editor challenge](https://x.com/Elmiillor/status/2100601890383835200),
use source footage from **September 17–October 1, 2026 inclusive**. Generate one
long edit and two shorts per available VOD; review the results and upload the
best long video and two shorts for the submission before uploading the rest.

## Setup

Run commands from the repository root. Requires Python 3.12+, `uv`, and an
OpenAI key for real AI stages. FFmpeg and yt-dlp come with the dependencies.

```bash
uv sync
uv run milloclips --help
```

Put `OPENAI_API_KEY=...` in the gitignored `.env`. AI roles default to `gpt-5-nano`.

## 1. Generate or resume a VOD

Start with September 23's **ELISE BUFFS**:

```bash
uv run milloclips auto https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e
```

`auto` generates the edits and thumbnail without publishing. It prints the path
to `auto-summary.json`, which lists the outputs. Two directories matter:

- **VOD workspace:** `workspace/<vod-id>` contains source metadata, scan/ranking,
  `status.json`, and `auto-summary.json`.
- **Edit workspace:** the summary's **`workspace`** field, usually
  `workspace/<vod-id>/game-analyses/<game-id>`, contains the outputs to review/upload.

To resume after an interruption, rerun `auto` with the VOD workspace:

```bash
uv run milloclips auto "workspace/<vod-id>"
```

Matching analysis and renders are reused. `status.json` records a failure's stage
and error. `--force` refreshes analysis/edit decisions while retaining downloaded
media, matching transcripts, and unchanged paid image requests.

## 2. Review the outputs

Open the summary's video, report, and contact-sheet paths. Watch the full long
video and both shorts; inspect `long-transitions.mp4` for cuts and audio joins.
Choose the thumbnail/title before uploading:

```bash
# Replace this path with auto-summary.json's workspace value.
EDIT="workspace/<vod-id>/game-analyses/<game-id>"
uv run milloclips thumbnails "$EDIT" --choose 2
```

Candidate 1 is provisional by default. `--choose` selects an existing candidate
without an AI call or image regeneration. For revisions and feedback, see
`uv run milloclips thumbnails --help`.

## 3. Upload one reviewed video to YouTube

First follow [Google OAuth setup](docs/YOUTUBE.md) for **@agustinbravop**, then:

```bash
uv run milloclips youtube-auth --client-secrets .youtube/client_secret.json
uv run milloclips publish "$EDIT" --upload --clip long --approve
```

Use `--clip short-01` or `short-02` for a short. Uploads reuse pipeline titles;
long uploads also attach the chosen thumbnail. YouTube automatically classifies
eligible vertical videos as Shorts.

**`--approve` means “I reviewed this output; proceed.”** It does not perform a
review, change YouTube visibility, or request YouTube's approval.

Uploads request **Unlisted / Oculto**, but unaudited API projects may force
**Private / Privado**. Check the printed link in Studio, change it to Unlisted
if needed, and verify playback signed out. Test this on one upload first.
Retry the same command after interruptions or daily limits; progress is saved.

## 4. Link the confirmed upload to the gallery

```bash
uv run milloclips catalog-sync --since 2026-09-17 --until 2026-10-01
uv run milloclips publish "$EDIT" --confirm-youtube --clip long --approve
```

Confirmation checks Unlisted visibility, processing, and embedding, then updates
one gallery clip while preserving the others. Use the same `--clip` as the upload.
Deploy the website separately to make the catalog change live.

Plain `publish "$EDIT" --approve` instead publishes **all local clips** and
replaces that VOD's gallery clip records. Use `--confirm-youtube` for this YouTube workflow.

## Generate the eligible League VOD batch

```bash
uv run milloclips vods --league-only --since 2026-09-17 --until 2026-10-01 --limit 1000 --json \
  | uv run python -c 'import json,sys; [print(v["url"]) for v in json.load(sys.stdin)]' \
  | xargs -P 3 -I{} uv run milloclips auto {}
```

This generates available League-category VODs returned by Kick, at concurrency 3;
`--limit 1000` avoids the default ten-result cap. Finish the first VOD before
starting this batch to warm shared assets. Review outputs and upload serially;
the batch does not publish anything.

## Help and checks

Each command's `--help` explains its inputs, outputs, flags, and examples:

```bash
uv run milloclips auto --help
uv run milloclips publish --help
uv run milloclips fixture                     # synthetic pipeline, no API key
uv run python -m unittest discover -s tests
```

- [YouTube setup and recovery](docs/YOUTUBE.md)
- [Editing rules, caching, and artifact contracts](docs/ARCHITECTURE.md)
- [Gallery development and deployment](website/README.md) — separate SvelteKit app; use `bun`.
