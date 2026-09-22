from __future__ import annotations

import html
from pathlib import Path

from .io import format_time, read_json


CSS = """
:root{--ink:#10110e;--paper:#e9e4d2;--acid:#d6ff00;--red:#ff3b30;--muted:#756f60}
*{box-sizing:border-box}body{margin:0;background:var(--ink);color:var(--paper);font-family:Georgia,serif}
header{padding:48px max(24px,6vw) 28px;border-bottom:1px solid #444;background:repeating-linear-gradient(135deg,#10110e 0 14px,#161812 14px 28px)}
h1,h2,.label{font-family:Impact,"Arial Narrow",sans-serif;text-transform:uppercase;letter-spacing:.04em;margin:0}
h1{font-size:clamp(3rem,9vw,8rem);line-height:.82;color:var(--acid)}
.dek{max-width:680px;font-size:1.1rem;color:#b9b4a5;margin-top:22px}.timeline{display:flex;height:54px;margin-top:32px;border:2px solid var(--paper)}
.slice{min-width:2px;border-right:1px solid #111}.IN_GAME{background:var(--acid)}.CHAMP_SELECT{background:#58a6ff}.LOADING{background:#f4a261}.POST_GAME{background:var(--red)}.OTHER,.QUEUE{background:#333}
main{padding:36px max(24px,6vw) 80px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:18px}.card{border:1px solid #59584f;background:#191a16;padding:18px;box-shadow:8px 8px 0 #000}.card h2{font-size:2rem;color:var(--acid)}.card img{width:100%;aspect-ratio:16/9;object-fit:cover;filter:saturate(.8) contrast(1.08);border:1px solid #59584f;margin-top:14px}
.time{font:700 1.3rem "Courier New",monospace;margin:12px 0}.meta{color:#aaa493}.badge{display:inline-block;background:var(--paper);color:var(--ink);font:700 .72rem "Courier New",monospace;padding:5px 7px;margin:4px 4px 0 0;text-transform:uppercase}.empty{border:1px dashed var(--muted);padding:32px;color:#aaa493}
"""


def scan_report(vod_dir: Path) -> Path:
    manifest = read_json(vod_dir / "manifest.json")
    states = read_json(vod_dir / "stream-states.json")["points"]
    games_path = vod_dir / "games.reviewed.json"
    games = read_json(games_path if games_path.exists() else vod_dir / "games.json")["games"]
    total = manifest["duration"]
    slices = []
    for index, point in enumerate(states):
        end = states[index + 1]["timestamp"] if index + 1 < len(states) else total
        width = max(0.1, (end - point["timestamp"]) / total * 100)
        slices.append(f'<div class="slice {point["state"]}" style="width:{width:.3f}%" title="{format_time(point["timestamp"])} {point["state"]}"></div>')
    cards = []
    for game in games:
        nearest = min(states, key=lambda point: abs(point["timestamp"] - game["gameplay_start"])) if states else None
        image = f'<img src="{Path(nearest["frame"]).as_uri()}" alt="Gameplay near detected start">' if nearest else ""
        cards.append(
            f"""<article class="card"><h2>{html.escape(game['id'])}</h2>
<div class="time">{format_time(game['gameplay_start'])} → {format_time(game['gameplay_end'])}</div>
<p class="meta">Gameplay duration: {format_time(game['duration'])}</p>
<span class="badge">champ {format_time(game['champ_select_start']) if game['champ_select_start'] is not None else 'unknown'}</span>
<span class="badge">load {format_time(game['loading_start']) if game['loading_start'] is not None else 'unknown'}</span>{image}</article>"""
        )
    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Milloclips scan</title><style>{CSS}</style></head>
<body><header><div class="label">Iteration 0 / VOD scan</div><h1>Game<br>Contact Sheet</h1><p class="dek">{html.escape(str(manifest['metadata'].get('title') or manifest['vod_id']))} · {format_time(total)} · {len(games)} games detected</p><div class="timeline">{''.join(slices)}</div></header>
<main><div class="grid">{''.join(cards) if cards else '<div class="empty">No games detected. Review stream-states.json before continuing.</div>'}</div></main></body></html>"""
    output = vod_dir / "reports" / "scan-report.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output


def shorts_report(vod_dir: Path) -> Path:
    candidates = read_json(vod_dir / "short-candidates.json")["candidates"]
    cards = [
        f"""<article class="card"><h2>{html.escape(item['title'])}</h2><div class="time">{format_time(item['source_start'])} → {format_time(item['source_end'])}</div><p>{html.escape(item['reason'])}</p><span class="badge">score {item['score']}</span></article>"""
        for item in candidates
    ]
    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Milloclips shorts</title><style>{CSS}</style></head><body><header><div class="label">Iteration 0 / Shorts</div><h1>Vertical<br>Candidates</h1><p class="dek">League-only moments ranked for a self-contained setup and payoff.</p></header><main><div class="grid">{''.join(cards) if cards else '<div class="empty">No candidates found.</div>'}</div></main></body></html>"""
    output = vod_dir / "reports" / "short-candidates.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output
