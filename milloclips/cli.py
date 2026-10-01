from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .ai import AI, THUMBNAIL_PREFERENCES
from .editing import analyze, find_shorts, games, plan_long, render_long, render_long_review, render_short, render_shorts, select_game
from .fixture import create_fixture
from .io import read_json, record_run, write_json
from .kick import print_vods, recent_vods
from .media import ingest
from .reports import scan_report, shorts_report
from .scan import scan
from .thumbnails import prepare_thumbnail_references, render_thumbnails, revise_thumbnail, select_thumbnail


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="milloclips", description="Iteration-zero AI VOD editing pipeline")
    commands = root.add_subparsers(dest="command", required=True)

    ingest_parser = commands.add_parser("ingest", help="Download a Kick VOD or register a local media file")
    ingest_parser.add_argument("source")
    ingest_parser.add_argument("--root", type=Path, default=Path("workspace"))

    auto = commands.add_parser("auto", help="Run a VOD through analysis, editing, and rendering")
    auto.add_argument("source", help="Kick URL, local video, or existing VOD workspace")
    auto.add_argument("--root", type=Path, default=Path("workspace"))
    auto.add_argument("--force", action="store_true")
    auto.add_argument("--model", default="gpt-5-nano")
    auto.add_argument("--editorial-model")

    vods = commands.add_parser("vods", help="List recent Kick VODs without downloading them")
    vods.add_argument("channel", nargs="?", default="elmiillor")
    vods.add_argument("--limit", type=int, default=10)
    vods.add_argument("--league-only", action="store_true")

    for name, help_text in [
        ("scan", "Detect League games and generate the scanner report"),
        ("analyze", "Transcribe and classify beats in the selected game"),
        ("plan-long", "Create a duration-constrained horizontal edit plan"),
        ("find-shorts", "Find and report vertical short candidates"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("vod_dir", type=Path)
        command.add_argument("--mock", action="store_true")
        command.add_argument("--force", action="store_true")
        command.add_argument("--model", default="gpt-5-nano", help="Model for frame and beat analysis")
        command.add_argument("--editorial-model", help="Model for long-form, short, and thumbnail editorial decisions")

    select = commands.add_parser("select", help="Select a detected game")
    select.add_argument("vod_dir", type=Path)
    select.add_argument("game_id")

    long_render = commands.add_parser("render-long", help="Render the horizontal rough cut")
    long_render.add_argument("vod_dir", type=Path)

    long_review = commands.add_parser("review-long", help="Render a short reel of every long-form transition")
    long_review.add_argument("vod_dir", type=Path)

    short_render = commands.add_parser("render-short", help="Render a vertical short")
    short_render.add_argument("vod_dir", type=Path)
    short_render.add_argument("--candidate", type=int, default=0)

    render_all_shorts = commands.add_parser("render-shorts", help="Render every passing vertical short")
    render_all_shorts.add_argument("vod_dir", type=Path)

    thumbnails = commands.add_parser("thumbnails", help="Generate three evidence-backed thumbnail candidates and a contact sheet")
    thumbnails.add_argument("vod_dir", type=Path)
    thumbnails.add_argument("--faces", type=Path, default=Path("assets/faces"))
    thumbnails.add_argument("--force", action="store_true")
    choice = thumbnails.add_mutually_exclusive_group()
    choice.add_argument("--choose", type=int, choices=(1, 2, 3), help="Select an existing candidate without regenerating")
    choice.add_argument("--choose-ai", action="store_true", help="Compare existing candidates with AI and select a winner")
    choice.add_argument("--feedback", help="Save human thumbnail feedback; does not render")
    choice.add_argument("--prepare-references", action="store_true", help="Cache the channel reference thumbnails without calling AI")
    choice.add_argument("--revise", type=int, choices=(1, 2, 3), help="Edit one generated candidate, preserving its original assets")
    thumbnails.add_argument("--edit", help="Targeted image-edit instruction, used with --revise")
    thumbnails.add_argument("--headline", help="Optional reviewed replacement headline, used with --revise")
    thumbnails.add_argument("--feedback-scope", choices=("channel", "video"), default="channel",
                            help="Apply feedback across videos (default) or only this VOD workspace")
    thumbnails.add_argument("--mock", action="store_true")
    thumbnails.add_argument("--image-budget", type=float, default=5, help="Cumulative image-generation spend/reservation limit for this workspace, in USD")
    thumbnails.add_argument("--model", default="gpt-5-nano", help="Model for frame and beat analysis")
    thumbnails.add_argument("--editorial-model", help="Model for long-form, short, and thumbnail editorial decisions")

    fixture = commands.add_parser("fixture", help="Run a synthetic Iteration 0 pipeline without an API key")
    fixture.add_argument("--root", type=Path, default=Path("/tmp/milloclips-fixture"))
    fixture.add_argument("--seconds", type=int, default=1200)
    fixture.add_argument("--render", action="store_true")
    fixture.add_argument("--force", action="store_true")
    return root


def main() -> None:
    load_dotenv()
    arguments = parser().parse_args()
    if arguments.command == "vods":
        if arguments.limit < 1:
            raise SystemExit("--limit must be at least 1")
        print_vods(recent_vods(arguments.channel, arguments.limit, arguments.league_only))
        return
    if arguments.command == "ingest":
        print(ingest(arguments.source, arguments.root))
        return
    if arguments.command == "auto":
        source_path = Path(arguments.source).expanduser()
        if source_path.is_dir() and (source_path / "manifest.json").exists():
            vod_dir = source_path.resolve()
        else:
            vod_dir = ingest(arguments.source, arguments.root)
        ai = AI(vod_dir, model=arguments.model, editorial_model=arguments.editorial_model)
        scan(vod_dir, ai, force=arguments.force)
        games_found = games(vod_dir)
        record_run(vod_dir, "scan", model=ai.model)
        game_report = scan_report(vod_dir)
        if not games_found:
            raise SystemExit(f"No eligible games detected. See {game_report}")

        analyses = []
        for game in games_found:
            game_dir = vod_dir / "game-analyses" / game["id"]
            game_dir.mkdir(parents=True, exist_ok=True)
            write_json(game_dir / "manifest.json", read_json(vod_dir / "manifest.json"))
            write_json(game_dir / "games.json", {"games": [game]})
            select_game(game_dir, game["id"])
            game_ai = AI(game_dir, model=arguments.model, editorial_model=arguments.editorial_model)
            beats = analyze(game_dir, game_ai, force=arguments.force)
            record_run(game_dir, "analyze", model=game_ai.model)
            analyses.append({"game_id": game["id"], "duration": game["duration"], "beats": beats})

        ranking = ai.rank_games(analyses)
        write_json(vod_dir / "game-ranking.json", {"ranking": ranking})
        winner_id = ranking[0]["game_id"]
        winner_dir = vod_dir / "game-analyses" / winner_id
        select_game(vod_dir, winner_id)
        game_ai = AI(winner_dir, model=arguments.model, editorial_model=arguments.editorial_model)
        plan_long(winner_dir, game_ai, force=arguments.force)
        record_run(winner_dir, "plan-long", model=game_ai.model)
        long_video = render_long(winner_dir)
        record_run(winner_dir, "render-long")
        transition_reel = winner_dir / "renders" / "long-transitions.mp4"
        record_run(winner_dir, "review-long")
        candidates = find_shorts(winner_dir, game_ai, force=arguments.force)
        record_run(winner_dir, "find-shorts", model=f"{game_ai.model}+{game_ai.editorial_model}")
        shorts = render_shorts(winner_dir)
        short_report = shorts_report(winner_dir)
        record_run(winner_dir, "render-shorts")
        thumbnail = render_thumbnails(winner_dir, Path("assets/faces"), arguments.force, game_ai)
        outputs = {
            "source_workspace": str(vod_dir), "selected_game": winner_id,
            "scan_report": str(game_report), "ranking": str(vod_dir / "game-ranking.json"),
            "long_video": str(long_video),
            "transition_review": str(transition_reel), "short_candidates": len(candidates),
            "shorts": [str(path) for path in shorts], "short_report": str(short_report),
            "thumbnail": str(winner_dir / "renders" / "thumbnail.png"),
            "thumbnail_contact_sheet": str(thumbnail),
        }
        write_json(vod_dir / "auto-summary.json", outputs)
        print(vod_dir / "auto-summary.json")
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
    if arguments.command == "review-long":
        print(render_long_review(vod_dir))
        record_run(vod_dir, "review-long")
        return
    if arguments.command == "render-short":
        print(render_short(vod_dir, arguments.candidate))
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
        find_shorts(vod_dir, ai, force=arguments.force)
        record_run(vod_dir, "find-shorts", model=f"{ai.model}+{ai.editorial_model}", mock=arguments.mock)
        print(shorts_report(vod_dir))
