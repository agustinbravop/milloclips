from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .ai import AI
from .editing import analyze, find_shorts, plan_long, render_long, render_long_review, render_short, render_shorts, select_game
from .fixture import create_fixture
from .io import record_run
from .kick import print_vods, recent_vods
from .media import ingest
from .reports import scan_report, shorts_report
from .scan import scan
from .thumbnails import render_thumbnails


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="milloclips", description="Iteration-zero AI VOD editing pipeline")
    commands = root.add_subparsers(dest="command", required=True)

    ingest_parser = commands.add_parser("ingest", help="Download a Kick VOD or register a local media file")
    ingest_parser.add_argument("source")
    ingest_parser.add_argument("--root", type=Path, default=Path("workspace"))

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
    thumbnails.add_argument("--mock", action="store_true")

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
        record_run(vod_dir, "find-shorts", model=f"{ai.model}+{ai.shorts_model}", mock=True)
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
        print(render_thumbnails(vod_dir, arguments.faces, arguments.force, AI(vod_dir, mock=arguments.mock)))
        return

    ai = AI(vod_dir, mock=arguments.mock)
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
        record_run(vod_dir, "find-shorts", model=f"{ai.model}+{ai.shorts_model}", mock=arguments.mock)
        print(shorts_report(vod_dir))
