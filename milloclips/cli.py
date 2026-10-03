from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from .ai import AI, THUMBNAIL_PREFERENCES
from .editing import analyze, find_shorts, games, plan_long, render_long, render_long_review, render_short, render_shorts, select_game
from .fixture import create_fixture
from .io import read_json, record_run, write_json
from .kick import kick_vods, print_vods
from .catalog import sync_catalog
from .media import ingest, vod_directory
from .publish import publish, publish_confirmed
from .reports import scan_report, shorts_report
from .scan import scan
from .pipeline import find_vod_shorts, game_workspace, rank_vod_games
from .editing import _fingerprint
from .thumbnails import prepare_thumbnail_references, render_thumbnails, revise_thumbnail, select_thumbnail


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="milloclips", description="Turn Kick VODs into a long edit, two shorts, and a thumbnail.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Workflow: vods -> auto -> review outputs -> publish --upload -> publish --confirm-youtube\n"
               "Run milloclips COMMAND --help for inputs, outputs, defaults, and examples.\n"
               "Real AI stages require OPENAI_API_KEY in .env. Paths are relative to the current directory.\n"
               "README.md: quickstart. docs/YOUTUBE.md: Google setup. docs/ARCHITECTURE.md: internals.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    def command(name: str, summary: str, details: str, examples: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=summary, description=f"{summary}.\n{details}",
                                   epilog=f"Examples (replace example paths with your own):\n{examples}",
                                   formatter_class=argparse.RawDescriptionHelpFormatter)

    ingest_parser = command("ingest", "Download a Kick VOD or register a local video",
        "Creates a cached source, metadata, and manifest.json; prints the VOD workspace.\n"
        "Kick URLs require network access; local files require no AI key.",
        "  uv run milloclips ingest https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e\n"
        "  uv run milloclips ingest /path/to/video.mp4")
    ingest_parser.add_argument("source", help="Kick VOD URL or existing local video file")
    ingest_parser.add_argument("--root", type=Path, default=Path("workspace"), help="Parent of VOD workspaces (default: workspace)")

    auto = command("auto", "Generate or resume all outputs for one VOD",
        "Requires OPENAI_API_KEY. Scans and ranks all games for the long edit;\n"
        "selects two shorts independently across the entire VOD, including between games.\n"
        "Writes status.json and auto-summary.json in the source VOD workspace.\n"
        "The summary's workspace field locates the edit directory for review and publish.\n"
        "Reuses matching caches and renders; never uploads or publishes.",
        "  uv run milloclips auto https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e\n"
        "  uv run milloclips auto workspace/VOD")
    auto.add_argument("source", help="Kick URL, local video, or existing VOD workspace")
    auto.add_argument("--root", type=Path, default=Path("workspace"), help="Workspace parent for new inputs; ignored for an existing workspace (default: workspace)")
    auto.add_argument("--force", action="store_true", help="Refresh analysis, ranking, edit plans, and thumbnail concepts; reuse downloaded media, matching transcripts, and unchanged paid images")
    auto.add_argument("--model", default="gpt-5-nano", help="Frame/beat analysis model (default: gpt-5-nano)")
    auto.add_argument("--editorial-model", help="Ranking, editing, and thumbnail decisions model (default: --model)")

    vods = command("vods", "List available Kick VODs without downloading media",
        "Queries Kick; prints dates, durations, titles, and URLs. No AI key required.\n"
        "Date bounds are inclusive UTC start dates. --limit applies after filtering.",
        "  uv run milloclips vods --league-only --since 2026-09-17 --until 2026-10-01 --limit 1000\n"
        "  uv run milloclips vods --json")
    vods.add_argument("channel", nargs="?", default="elmiillor", help="Kick channel name (default: elmiillor)")
    vods.add_argument("--limit", type=int, default=10, help="Maximum returned VODs; raise for a full batch (default: 10)")
    vods.add_argument("--league-only", action="store_true", help="Include only VODs categorized as League of Legends")
    vods.add_argument("--since", help="Inclusive earliest UTC start date, YYYY-MM-DD (default: no lower bound)")
    vods.add_argument("--until", help="Inclusive latest UTC start date, YYYY-MM-DD (default: no upper bound)")
    vods.add_argument("--json", action="store_true", help="Emit JSON for batch drivers")

    catalog = command("catalog-sync", "Add missing League VODs to the gallery catalog",
        "Requires an existing SITE/static/catalog.json. Queries Kick and adds empty\n"
        "stream entries for later publishing; preserves existing entries. No AI calls.",
        "  uv run milloclips catalog-sync --since 2026-09-17 --until 2026-10-01")
    catalog.add_argument("--site", type=Path, default=Path("website"), help="Gallery app directory (default: website)")
    catalog.add_argument("--channel", default="elmiillor", help="Kick channel name (default: elmiillor)")
    catalog.add_argument("--since", help="Inclusive earliest UTC start date, YYYY-MM-DD (default: no lower bound)")
    catalog.add_argument("--until", help="Inclusive latest UTC start date, YYYY-MM-DD (default: no upper bound)")

    for name, summary, details, directory_help, force_help, example in [
        ("scan", "Detect League games and generate a review report",
         "Requires ingest first. Uses AI frame analysis; writes games.json,\nstream-states.json, and reports/scan-report.html. Does not select a winner.",
         "Source VOD workspace containing manifest.json",
         "Repeat scan analysis and frame sampling; keep downloaded source",
         "workspace/VOD"),
        ("analyze", "Transcribe and classify the selected game's moments",
         "Requires scan plus select, or a selected game workspace from rank-games.\nUses AI transcription/classification; writes transcript.json, beats.json, and layout.json.",
         "Workspace with manifest.json, games.json, and selection.json",
         "Reclassify moments; reuse matching transcripts and sampled frames",
         "workspace/VOD/game-analyses/game-001"),
        ("plan-long", "Plan a duration-constrained long edit",
         "Requires analyze in this directory. Uses AI editorial decisions;\nwrites long-plan.json and boundary/review artifacts. Does not render video.",
         "Selected edit workspace containing beats.json",
         "Refresh editorial selection and cut decisions; reuse media and transcripts",
         "workspace/VOD/game-analyses/game-001"),
        ("find-shorts", "Select two shorts independently across the entire VOD",
         "Requires an ingested/scanned VOD. Reuses game evidence and analyzes\nopening, between-game, and closing footage. Uses AI; writes vod-shorts/\nevidence, a short-candidates.json link, and reports/short-candidates.html.\nAccepts the source VOD or its game-analyses edit directory; does not render.",
         "Source VOD workspace or game-analyses edit directory receiving the selection",
         "Refresh whole-VOD analysis and short decisions; reuse matching transcripts and media",
         "workspace/VOD/game-analyses/game-001"),
        ("rank-games", "Analyze and rank every detected game for the long edit",
         "Requires scan first. Uses AI; caches each game's analysis in game-analyses/,\nwrites game-ranking.json, selects the winner, and prints its workspace as JSON.\nDoes not render videos or choose shorts.",
         "Source VOD workspace containing games.json",
         "Refresh game classifications and ranking; reuse matching transcripts and media",
         "workspace/VOD"),
    ]:
        stage_parser = command(name, summary, details + "\nRequires OPENAI_API_KEY unless --mock is used.",
                               f"  uv run milloclips {name} {example}")
        stage_parser.add_argument("vod_dir", type=Path, help=directory_help)
        stage_parser.add_argument("--mock", action="store_true", help="Use fixture AI replies without an API key; local media processing still runs")
        stage_parser.add_argument("--force", action="store_true", help=force_help)
        stage_parser.add_argument("--model", default="gpt-5-nano", help="Frame/beat analysis model (default: gpt-5-nano)")
        stage_parser.add_argument("--editorial-model", help="Ranking/editing decisions model (default: --model)")

    select = command("select", "Manually select a detected game",
        "Requires scan first. Writes selection.json; prints the selected id.\nUse an id from games.json or the scan report. No AI calls or rendering.",
        "  uv run milloclips select workspace/VOD game-001")
    select.add_argument("vod_dir", type=Path, help="VOD workspace containing games.json")
    select.add_argument("game_id", help="Detected game id, e.g. game-001; not a numeric index")

    long_render = command("render-long", "Render the 1280x720 long edit with audio and cards",
        "Requires a current long-plan.json from plan-long and the local source.\nWrites renders/long-rough.mp4 and its validation sidecar; new renders also\ngenerate the transition reel. Reuses a matching render. No AI calls.",
        "  uv run milloclips render-long workspace/VOD/game-analyses/game-001")
    long_render.add_argument("vod_dir", type=Path, help="Selected edit workspace containing long-plan.json")

    long_review = command("render-long-review", "Render a reel of the long edit's transitions",
        "Requires renders/long-rough.mp4 and its long-plan.json. Writes\nrenders/long-transitions.mp4 and long-review.json, with eight seconds around\neach join. This creates review material; it does not approve the video. No AI calls.",
        "  uv run milloclips render-long-review workspace/VOD/game-analyses/game-001")
    long_review.add_argument("vod_dir", type=Path, help="Edit workspace containing the rendered long video and its plan")

    short_render = command("render-short", "Render one selected short at 1080x1920 with captions",
        "Requires find-shorts first and the local source. Writes renders/short-NN.mp4,\nits validation sidecar, and a refreshed short report; reuses a matching render.\n--candidate-index is zero-based: 0 is the first selected short, usually short-01.\nUses cached caption formatting or original transcript words. No AI calls.",
        "  uv run milloclips render-short workspace/VOD/game-analyses/game-001 --candidate-index 0\n"
        "  uv run milloclips render-short workspace/VOD/game-analyses/game-001 --candidate-index 1")
    short_render.add_argument("vod_dir", type=Path, help="Output workspace containing short-candidates.json")
    short_render.add_argument("--candidate-index", type=int, default=0, help="Zero-based selected candidate index; 0 = first, 1 = second (default: 0)")

    render_all_shorts = command("render-shorts", "Render all selected shorts with captions",
        "Requires find-shorts first. Writes validated renders/short-NN.mp4 files\nand refreshes the short report; matching renders are reused. Uses cached\ncaption formatting or original transcript words. No AI calls or uploads.",
        "  uv run milloclips render-shorts workspace/VOD/game-analyses/game-001")
    render_all_shorts.add_argument("vod_dir", type=Path, help="Output workspace containing short-candidates.json")

    thumbnails = command("thumbnails", "Generate, choose, or revise a long-video thumbnail",
        "Generation requires the long plan, beats, face assets, and OPENAI_API_KEY.\nWrites renders/thumbnails/ candidates/contact sheet and renders/thumbnail.png.\nCandidate 1 is provisional until chosen. --choose is local; --choose-ai uses AI;\n--revise makes a paid image edit. --feedback only saves notes; --prepare-references\nonly downloads reference thumbnails. Original artwork and reviewed edits stay separate.",
        "  uv run milloclips thumbnails workspace/VOD/game-analyses/game-001\n"
        "  uv run milloclips thumbnails workspace/VOD/game-analyses/game-001 --choose 2")
    thumbnails.add_argument("vod_dir", type=Path, help="Selected edit workspace; --prepare-references still requires this positional path")
    thumbnails.add_argument("--faces", type=Path, default=Path("assets/faces"), help="Reusable reaction photos and framing metadata (default: assets/faces)")
    thumbnails.add_argument("--force", action="store_true", help="Refresh concepts; reuse downloaded references/assets and unchanged paid image requests")
    choice = thumbnails.add_mutually_exclusive_group()
    choice.add_argument("--choose", type=int, choices=(1, 2, 3), help="Choose an existing candidate number (one-based); no AI or regeneration")
    choice.add_argument("--choose-ai", action="store_true", help="Compare existing candidates with AI and select a winner")
    choice.add_argument("--feedback", help="Save human thumbnail feedback; does not render")
    choice.add_argument("--prepare-references", action="store_true", help="Cache the channel reference thumbnails without calling AI")
    choice.add_argument("--revise", type=int, choices=(1, 2, 3), help="Edit one generated candidate, preserving its original assets")
    thumbnails.add_argument("--edit", help="Targeted image-edit instruction, used with --revise")
    thumbnails.add_argument("--headline", help="Optional reviewed replacement headline, used with --revise")
    thumbnails.add_argument("--feedback-scope", choices=("channel", "video"), default="channel",
                            help="Scope of --feedback: all videos or this edit workspace (default: channel)")
    thumbnails.add_argument("--mock", action="store_true", help="Generate fixture thumbnails without AI calls or reference downloads")
    thumbnails.add_argument("--image-budget", type=float, default=5, help="Cumulative image spend/reservation ceiling in USD, not a provider billing cap (default: 5)")
    thumbnails.add_argument("--model", default="gpt-5-nano", help="Frame/beat analysis model (default: gpt-5-nano)")
    thumbnails.add_argument("--editorial-model", help="Thumbnail concept/art-direction model; not the image generator (default: --model)")

    publish_parser = command("publish", "Upload a reviewed clip or update the gallery",
        "Input: edit directory from auto-summary.json's workspace field. All modes require --approve.\n"
        "Modes:\n"
        "  No action flag: publish all available local clips to SITE/static/catalog.json.\n"
        "    Requires a matching catalog entry and validated renders; replaces its clip records.\n"
        "  --upload --clip NAME: upload one video/title to YouTube; attach a thumbnail for long.\n"
        "    Requires youtube-auth and reviewed renders. Saves youtube/NAME.json; no gallery change.\n"
        "    Requests Unlisted; unaudited API projects may force Private. Retry the same command to resume.\n"
        "  --confirm-youtube --clip NAME: after checking signed-out playback, verify Unlisted,\n"
        "    processing, and embedding; link the saved upload in the gallery, preserving other clips.\n"
        "YouTube modes call the API. Local mode does not. Gallery changes need a separate site deploy.\n"
        "Run uploads serially; set Private -> Unlisted in Studio if needed. Setup: docs/YOUTUBE.md.",
        "  uv run milloclips publish workspace/VOD/game-analyses/game-001 --upload --clip short-01 --approve\n"
        "  uv run milloclips publish workspace/VOD/game-analyses/game-001 --confirm-youtube --clip short-01 --approve")
    publish_parser.add_argument("vod_dir", type=Path, help="Reviewed edit workspace containing renders and manifest.json")
    publish_parser.add_argument("--site", type=Path, default=Path("website"), help="Gallery app directory; unused during --upload (default: website)")
    publish_parser.add_argument("--approve", action="store_true", help="Required: you reviewed the output; with --confirm-youtube, you checked Unlisted playback. Does not change visibility or ask YouTube for approval")
    publish_parser.add_argument("--youtube", help="Local-gallery mode only: use an existing long-video id instead of a local file; does not upload or verify visibility")
    publish_parser.add_argument("--slug", help="Local-gallery asset basename (default: generated thumbnail title slug); not allowed with YouTube actions")
    publish_parser.add_argument("--vod-id", help="Catalog VOD id override when the manifest Kick URL cannot match an entry; unused during --upload")
    youtube_action = publish_parser.add_mutually_exclusive_group()
    youtube_action.add_argument("--upload", action="store_true", help="Upload one reviewed clip to YouTube; does not change the gallery")
    youtube_action.add_argument("--confirm-youtube", action="store_true", help="Check Unlisted processing and link one saved upload to the gallery")
    publish_parser.add_argument("--clip", help="Required with YouTube actions: long or a short's render basename, e.g. short-01; not a candidate index. YouTube automatically classifies eligible vertical videos as Shorts")
    publish_parser.add_argument("--auth-dir", type=Path, default=Path(".youtube"), help="Saved Google OAuth credentials; YouTube modes only (default: .youtube)")

    auth = command("youtube-auth", "Authorize your personal YouTube channel",
        "Requires YouTube Data API enabled and a Desktop app OAuth client JSON.\nOpens a browser with a localhost callback, verifies the selected channel,\nand saves credentials locally. Does not upload. Setup: docs/YOUTUBE.md.",
        "  uv run milloclips youtube-auth --client-secrets .youtube/client_secret.json")
    auth.add_argument("--client-secrets", type=Path, required=True, help="Google Cloud desktop OAuth client JSON")
    auth.add_argument("--auth-dir", type=Path, default=Path(".youtube"), help="Credential storage; gitignored at the default location (default: .youtube)")
    auth.add_argument("--channel", default="@agustinbravop", help="Expected channel handle or id; rejects the wrong channel (default: @agustinbravop)")

    fixture = command("fixture", "Run the synthetic editing fixture without an API key",
        "Creates or reuses synthetic local media; mocks scanning, analysis, long planning,\nand selected-game shorts discovery. Prints report paths and the fixture workspace.\nDoes not exercise real model quality, whole-VOD ranking, thumbnails, or uploads.",
        "  uv run milloclips fixture\n"
        "  uv run milloclips fixture --root /tmp/milloclips-fixture --render")
    fixture.add_argument("--root", type=Path, default=Path("/tmp/milloclips-fixture"), help="Fixture media/workspace directory (default: /tmp/milloclips-fixture)")
    fixture.add_argument("--seconds", type=int, default=1200, help="Synthetic source duration in seconds; ignored if source exists (default: 1200)")
    fixture.add_argument("--render", action="store_true", help="Also render the long video and selected shorts with FFmpeg")
    fixture.add_argument("--force", action="store_true", help="Refresh fixture analysis and edit decisions; reuse the synthetic source")
    return root


def _write_status(vod_dir: Path, stage: str, ok: bool, error: str | None = None) -> None:
    write_json(vod_dir / "status.json", {
        "stage": stage, "ok": ok, "error": error,
        "source_workspace": str(vod_dir), "updated_at": datetime.now(UTC).isoformat(),
    })


def _auto(arguments: argparse.Namespace) -> Path:
    source_path = Path(arguments.source).expanduser()
    if source_path.is_dir() and (source_path / "manifest.json").exists():
        vod_dir = source_path.resolve()
        first_stage = "scan"
    else:
        vod_dir = vod_directory(arguments.source, arguments.root)
        first_stage = "ingest"
    status: dict = {"stage": first_stage, "ok": None, "error": None, "source_workspace": str(vod_dir)}

    def stage(name: str) -> None:
        status.update(stage=name)
        write_json(vod_dir / "status.json", {**status, "updated_at": datetime.now(UTC).isoformat()})

    try:
        if first_stage == "ingest":
            ingest(arguments.source, arguments.root)
        ai = AI(vod_dir, model=arguments.model, editorial_model=arguments.editorial_model)
        scan(vod_dir, ai, force=arguments.force)
        games_found = games(vod_dir)
        record_run(vod_dir, "scan", model=ai.model)
        game_report = scan_report(vod_dir)
        if not games_found:
            raise SystemExit(f"No eligible games detected. See {game_report}")

        analyses = []
        for game in games_found:
            stage(f"analyze:{game['id']}")
            game_dir = game_workspace(vod_dir, game)
            game_ai = AI(game_dir, model=arguments.model, editorial_model=arguments.editorial_model)
            beats = analyze(game_dir, game_ai, force=arguments.force)
            record_run(game_dir, "analyze", model=game_ai.model)
            analyses.append({"game_id": game["id"], "duration": game["duration"], "beats": beats})

        stage("rank-games")
        ranking_path = vod_dir / "game-ranking.json"
        ranking_signature = _fingerprint({"analyses": analyses, "model": ai.editorial_model, "ranking_version": 2})
        cached_ranking = read_json(ranking_path) if ranking_path.exists() else {}
        ranking = cached_ranking["ranking"] if not arguments.force and cached_ranking.get("signature") == ranking_signature else ai.rank_games(analyses)
        write_json(ranking_path, {"signature": ranking_signature, "ranking": ranking})
        winner_id = ranking[0]["game_id"]
        winner_dir = vod_dir / "game-analyses" / winner_id
        select_game(vod_dir, winner_id)
        game_ai = AI(winner_dir, model=arguments.model, editorial_model=arguments.editorial_model)

        stage("plan-long")
        plan_long(winner_dir, game_ai, force=arguments.force)
        record_run(winner_dir, "plan-long", model=game_ai.model)
        stage("render-long")
        long_video = render_long(winner_dir)
        record_run(winner_dir, "render-long")
        transition_reel = winner_dir / "renders" / "long-transitions.mp4"
        record_run(winner_dir, "render-long-review")
        stage("find-shorts")
        candidates = find_vod_shorts(vod_dir, ai, force=arguments.force, output_dir=winner_dir)
        record_run(winner_dir, "find-shorts", model=f"{game_ai.model}+{game_ai.editorial_model}")
        stage("render-shorts")
        shorts = render_shorts(winner_dir)
        short_report = shorts_report(winner_dir)
        record_run(winner_dir, "render-shorts")
        stage("thumbnails")
        thumbnail = render_thumbnails(winner_dir, Path("assets/faces"), arguments.force, game_ai)
        record_run(winner_dir, "thumbnails", model=f"{game_ai.model}+{game_ai.editorial_model}")
        outputs = {
            "source_workspace": str(vod_dir), "workspace": str(winner_dir), "selected_game": winner_id,
            "scan_report": str(game_report), "ranking": str(vod_dir / "game-ranking.json"),
            "long_video": str(long_video),
            "transition_review": str(transition_reel), "short_candidates": len(candidates),
            "shorts": [str(path) for path in shorts], "short_report": str(short_report),
            "thumbnail": str(winner_dir / "renders" / "thumbnail.png"),
            "thumbnail_contact_sheet": str(thumbnail),
        }
        write_json(vod_dir / "auto-summary.json", outputs)
        status.update(ok=True)
        stage("done")
        return vod_dir / "auto-summary.json"
    except BaseException as error:
        _write_status(vod_dir, status["stage"], False, f"{type(error).__name__}: {error}")
        raise


def main() -> None:
    load_dotenv()
    arguments = parser().parse_args()
    if arguments.command == "vods":
        if arguments.limit < 1:
            raise SystemExit("--limit must be at least 1")
        vods = kick_vods(arguments.channel, arguments.league_only, arguments.since, arguments.until)[:arguments.limit]
        if arguments.json:
            print(json.dumps(vods, ensure_ascii=False, indent=2))
        else:
            print_vods(vods)
        return
    if arguments.command == "catalog-sync":
        for vod in sync_catalog(arguments.site, arguments.channel, arguments.since, arguments.until):
            print(f"{vod['date'][:10]}  {vod['title']}  {vod['id']}")
        return
    if arguments.command == "ingest":
        print(ingest(arguments.source, arguments.root))
        return
    if arguments.command == "publish":
        if arguments.upload or arguments.confirm_youtube:
            if not arguments.clip:
                raise SystemExit("--upload and --confirm-youtube require --clip long|short-01|short-02")
            if arguments.youtube or arguments.slug:
                raise SystemExit("--youtube and --slug cannot be combined with individual YouTube publishing")
            from .youtube import confirm_video, upload_video
            if arguments.upload:
                state = upload_video(arguments.vod_dir, arguments.clip, arguments.auth_dir, arguments.approve)
                print(state["url"])
                print("Check processing/playback and set Unlisted in Studio if needed; then run --confirm-youtube for this clip.")
            else:
                state = confirm_video(arguments.vod_dir, arguments.clip, arguments.auth_dir, arguments.approve)
                print(publish_confirmed(arguments.vod_dir, arguments.site, state, arguments.vod_id))
            return
        if arguments.clip:
            raise SystemExit("--clip requires --upload or --confirm-youtube")
        print(publish(arguments.vod_dir, arguments.site, arguments.approve,
                      arguments.youtube, arguments.slug, arguments.vod_id))
        return
    if arguments.command == "youtube-auth":
        from .youtube import authorize
        channel = authorize(arguments.client_secrets, arguments.auth_dir, arguments.channel)
        print(f"Authorized {channel['handle'] or channel['id']} ({channel['id']})")
        return
    if arguments.command == "auto":
        print(_auto(arguments))
        return
    if arguments.command == "ingest":
        print(ingest(arguments.source, arguments.root))
        return
    if arguments.command == "publish":
        print(publish(arguments.vod_dir, arguments.site, arguments.approve,
                      arguments.youtube, arguments.slug, arguments.vod_id))
        return
    if arguments.command == "fixture":
        vod_dir = create_fixture(arguments.root, arguments.seconds)
        ai = AI(vod_dir, mock=True)
        detected = scan(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "scan", model=ai.model, mock=True)
        print(scan_report(vod_dir))
        if not detected:
            raise SystemExit("Synthetic scanner did not detect a game")
        select_game(vod_dir, detected[0]["id"])
        analyze(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "analyze", model=ai.model, mock=True)
        plan_long(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "plan-long", model=ai.model, mock=True)
        find_shorts(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "find-shorts", model=f"{ai.model}+{ai.editorial_model}", mock=True)
        print(shorts_report(vod_dir))
        if arguments.render:
            print(render_long(vod_dir))
            for output in render_shorts(vod_dir):
                print(output)
            shorts_report(vod_dir)
            record_run(vod_dir, "render", mock=True)
        print(vod_dir)
        return

    vod_dir = arguments.vod_dir
    if arguments.command == "select":
        print(select_game(vod_dir, arguments.game_id)["id"])
        return
    if arguments.command == "render-long":
        print(render_long(vod_dir))
        record_run(vod_dir, "render-long")
        return
    if arguments.command == "render-long-review":
        print(render_long_review(vod_dir))
        record_run(vod_dir, "render-long-review")
        return
    if arguments.command == "render-short":
        print(render_short(vod_dir, arguments.candidate_index))
        record_run(vod_dir, "render-short")
        shorts_report(vod_dir)
        return
    if arguments.command == "render-shorts":
        for output in render_shorts(vod_dir):
            print(output)
        record_run(vod_dir, "render-shorts")
        shorts_report(vod_dir)
        return
    if arguments.command == "thumbnails":
        if (arguments.edit is not None or arguments.headline is not None) and arguments.revise is None:
            raise SystemExit("--edit and --headline require --revise")
        if arguments.revise is not None:
            if not arguments.edit:
                raise SystemExit("--revise requires --edit")
            print(revise_thumbnail(vod_dir, arguments.revise, arguments.edit, AI(vod_dir, model=arguments.model,
                  editorial_model=arguments.editorial_model, mock=arguments.mock), arguments.headline, arguments.image_budget))
            return
        if arguments.prepare_references:
            for reference in prepare_thumbnail_references():
                print(reference["path"])
            return
        if arguments.feedback is not None:
            if not arguments.feedback.strip():
                raise SystemExit("Thumbnail feedback must not be empty")
            target = THUMBNAIL_PREFERENCES if arguments.feedback_scope == "channel" else vod_dir / "thumbnail-feedback.reviewed.md"
            if arguments.feedback_scope == "video" and not vod_dir.is_dir():
                raise SystemExit("Video feedback requires an existing VOD workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(previous + "\n- " + arguments.feedback.strip() + "\n", encoding="utf-8")
            print(target)
            return
        if arguments.choose is not None:
            print(select_thumbnail(vod_dir, arguments.choose))
            return
        if arguments.choose_ai:
            print(select_thumbnail(vod_dir, ai=AI(vod_dir, model=arguments.model,
                  editorial_model=arguments.editorial_model, mock=arguments.mock)))
            return
        print(render_thumbnails(vod_dir, arguments.faces, arguments.force, AI(
            vod_dir, model=arguments.model, editorial_model=arguments.editorial_model, mock=arguments.mock,
        ), image_budget=arguments.image_budget))
        return

    ai = AI(vod_dir, model=arguments.model, editorial_model=arguments.editorial_model, mock=arguments.mock)
    if arguments.command == "scan":
        scan(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "scan", model=ai.model, mock=arguments.mock)
        print(scan_report(vod_dir))
    elif arguments.command == "analyze":
        analyze(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "analyze", model=ai.model, mock=arguments.mock)
        print(vod_dir / "beats.json")
    elif arguments.command == "plan-long":
        plan = plan_long(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "plan-long", model=ai.model, mock=arguments.mock)
        print(f"{vod_dir / 'long-plan.json'} ({plan['duration']:.1f}s)")
    elif arguments.command == "find-shorts":
        source_dir = vod_dir.parent.parent if vod_dir.parent.name == "game-analyses" else vod_dir
        find_vod_shorts(source_dir, ai, force=arguments.force, output_dir=vod_dir)
        record_run(vod_dir, "find-shorts", model=f"{ai.model}+{ai.editorial_model}", mock=arguments.mock)
        print(shorts_report(vod_dir))
    elif arguments.command == "rank-games":
        ranking, winner_dir = rank_vod_games(vod_dir, ai, force=arguments.force)
        print(json.dumps({"ranking": ranking, "workspace": str(winner_dir)}, ensure_ascii=False))
