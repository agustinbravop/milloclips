# Personal YouTube uploads

Target channel: **@agustinbravop**. All outputs should end up **Unlisted / Oculto**.
Generate edits in batches, review them, then upload **one individual video per
command**. Pick the best long video and two shorts after review and upload those
three submission videos before the rest. They can come from different VODs.

`--approve` confirms your review; it does not change visibility or request
YouTube approval. For all publishing flags, run `uv run milloclips publish --help`.

## One-time Google setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project
   and enable **YouTube Data API v3**.
2. In **Google Auth Platform**, configure the app's branding/support email,
   choose **External** audience, and add your personal Google email as a test user
   while the app is in **Testing**.
3. Configure these OAuth scopes:
   - `https://www.googleapis.com/auth/youtube.upload`: upload videos and thumbnails.
   - `https://www.googleapis.com/auth/youtube.readonly`: check the selected channel,
     visibility, processing and embedding eligibility.
4. Create an OAuth client with application type **Desktop app**. Download its
   JSON into `.youtube/client_secret.json` (create `.youtube/` first).
5. Install dependencies and authorize:

   ```bash
   uv sync
   uv run milloclips youtube-auth --client-secrets .youtube/client_secret.json
   ```

The command opens your browser and receives Google's callback on a temporary
localhost port. Choose the Google account/channel for **@agustinbravop**; a
different channel is rejected. Run the command on a machine with a browser that
can reach that same localhost. Tokens stay in `.youtube/`, which is gitignored;
the token file is restricted to the current OS user. Do not share this directory.
No Google password is given to the pipeline.

If Google shows an unverified-app warning for your own test app, verify the
project/client is yours before continuing. In External **Testing**, refresh
tokens for these scopes typically expire after seven days. Re-run `youtube-auth`
when authorization expires or is revoked. OAuth consent verification and the
YouTube API upload compliance audit are separate processes.

## First real test: one reviewed long video

Use the selected edit directory: `auto-summary.json`'s **workspace** field,
usually `workspace/<vod-id>/game-analyses/<selected-game>`.

```bash
uv run milloclips publish "<edit-directory>" --upload --clip long --approve
```

This uploads `renders/long-rough.mp4`, reuses the pipeline's selected/provisional
thumbnail title, and attaches a JPEG of `renders/thumbnail.png` resized to fit
1280×720 and checked against YouTube's 2 MB limit. The original PNG is retained.
Videos use Spanish language metadata, Gaming category, source credit in the
description, not-made-for-kids status, embedding enabled, and subscriber
notifications disabled. This workflow is for the existing League gameplay edits.

The command requests **Unlisted**. However, [YouTube restricts uploads from
unverified API projects created after July 28, 2020 to Private](https://developers.google.com/youtube/v3/docs/videos/insert).
This restriction requires a YouTube API compliance audit to lift; channel
verification/advanced features do not lift it.

Open the printed YouTube URL and find the upload in **YouTube Studio**:

1. Check that the title and thumbnail are correct.
2. Wait for processing and inspect any restrictions/checks in Studio.
3. If necessary, change visibility from **Private / Privado** to **Unlisted / Oculto**.
4. Test the link signed out or in an incognito window, including playback.

Prove that Studio permits this visibility change on the first upload before
uploading the rest. If it does not, use Studio uploads or obtain the API audit.

After checking playback:

```bash
uv run milloclips publish "<edit-directory>" --confirm-youtube --clip long --approve
```

Confirmation checks the saved video against your authorized channel and requires
Unlisted visibility, completed processing and embedding enabled. Only then does
it update the website's local `static/catalog.json` and stage the poster. The API
checks do not replace your signed-out playback check. Deploy the website through
its usual workflow to make the catalog update live.

## Individual shorts

Choose a validated render by its basename (`short-01`, `short-02`, etc.), **not**
the zero-based candidate index used by `render-short`:

```bash
uv run milloclips publish "<edit-directory>" --upload --clip short-01 --approve
# Check Unlisted visibility and signed-out playback in Studio.
uv run milloclips publish "<edit-directory>" --confirm-youtube --clip short-01 --approve
```

The title comes from that short's pipeline candidate. Shorts need a current
render sidecar, 1080×1920 video with audio, and duration ≤180 seconds. YouTube
classifies eligible vertical videos as Shorts using the same video upload
endpoint; there is no separate Shorts API. Custom thumbnails are attached only
to long videos. Confirming one output preserves the VOD's other gallery clips.

## Progress, retries and channel limits

- `<edit-directory>/youtube/<clip>.json` stores the channel, exact media hash,
  metadata, resumable session, returned video ID/URL and thumbnail progress.
  Writes are atomic. `youtube/long-thumbnail.jpg` is the uploaded gallery poster.
- After a connection failure, re-run **the same upload command**. It asks
  YouTube which bytes were accepted, resumes from that point, or recovers the
  video ID if the final response was lost. Thumbnail failures retry only the
  thumbnail. Completed uploads are reused.
- Changed media, title or thumbnail stops a retry rather than silently replacing
  an already-uploaded video. Restore the original artifacts to reuse its record.
  To intentionally replace an upload, inspect/delete the old video in Studio
  and archive its local record before starting a new upload.
- An expired resumable session stops without starting a replacement. Check
  Studio for the video before archiving/removing that record and trying again.
- A channel daily limit or API quota error stops with saved progress. Continue
  later with the same command. Without advanced channel features, do not assume
  all ~30 videos will fit in one day. Upload your three submission picks first.
- Run uploads serially; do not run two commands for the same clip concurrently.
  No batch upload queue or scheduled publishing is needed for this workflow.

The default auth directory is `.youtube`; `--auth-dir` can override it for auth,
upload and confirmation. Keep any alternative credential location outside git.
Use `--site` and `--vod-id` on confirmation if the gallery is elsewhere or the
manifest's Kick URL cannot identify its catalog entry. If needed, first run:

```bash
uv run milloclips catalog-sync --since 2026-09-17 --until 2026-10-01
```

The older `publish <edit-directory> --approve` command still publishes local
files for the whole VOD. Use the individual `--confirm-youtube` workflow for
YouTube-backed entries; whole-VOD local publishing replaces those clip records.
