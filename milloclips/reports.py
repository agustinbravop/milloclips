from __future__ import annotations

import html
from pathlib import Path

from .editing import _file_fingerprint, _fingerprint, _media_fingerprint, short_candidates
from .io import format_time, read_json


CSS = """
:root{color-scheme:dark;--bg:oklch(18% .012 105);--panel:oklch(23% .012 105);--ink:oklch(94% .009 105);--muted:oklch(76% .01 105);--line:oklch(39% .012 105);--accent:oklch(84% .18 117)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
header,main{width:min(100% - 40px,1020px);margin-inline:auto}
header{padding:48px 0 28px;border-bottom:1px solid var(--line)}
h1,h2,p{margin:0}h1{font-size:2.5rem;line-height:1.15;letter-spacing:-.035em;margin:10px 0 14px}
h2{font-size:1.35rem;line-height:1.3;letter-spacing:-.015em}
.label,.eyebrow,.fact dt{font-size:.78rem;font-weight:750;text-transform:uppercase;letter-spacing:.09em;color:var(--accent)}
.dek{max-width:70ch;color:var(--muted);overflow-wrap:anywhere}
main{padding-block:32px 72px}.stories{display:grid;gap:16px}.story,.game{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px}
.story-head{display:flex;align-items:flex-start;gap:18px}.rank{flex:none;color:var(--accent);font:700 1rem/1.5 ui-monospace,SFMono-Regular,monospace}
.story-content{min-width:0;max-width:75ch}.story-content h2{overflow-wrap:anywhere}
.time{font:600 .92rem/1.5 ui-monospace,SFMono-Regular,monospace;color:var(--muted);margin-top:8px;font-variant-numeric:tabular-nums}
.summary{margin:20px 0;overflow-wrap:anywhere}.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:0}
.fact{margin:0}.fact dt{margin-bottom:4px}.fact dd{margin:0;overflow-wrap:anywhere}
.segments{margin:18px 0 0;color:var(--muted);font-size:.9rem}.segments span{color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:16px}
.game img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px;margin-top:20px}
.game .facts{margin-top:18px}.empty{padding:32px;border:1px dashed var(--line);border-radius:8px;color:var(--muted)}
.evidence{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:18px}.evidence img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px}.status{display:inline-block;margin-top:10px;padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em}.status.valid{border-color:var(--accent);color:var(--accent)}
.timeline{display:flex;height:26px;margin-top:26px;border-radius:4px;overflow:hidden;background:var(--line)}
.slice{min-width:2px}.IN_GAME{background:var(--accent)}.CHAMP_SELECT{background:oklch(70% .12 235)}.LOADING{background:oklch(79% .13 70)}.POST_GAME{background:oklch(69% .16 25)}.OTHER,.QUEUE{background:var(--line)}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:10px;color:var(--muted);font-size:.84rem}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
@media(max-width:600px){header,main{width:min(100% - 32px,1020px)}header{padding-top:30px}h1{font-size:2rem}.story,.game{padding:18px}.facts{grid-template-columns:1fr}.story-head{gap:12px}}
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
            f"""<article class="game"><h2>{html.escape(game['id'])}</h2>
<p class="time">{format_time(game['gameplay_start'])} → {format_time(game['gameplay_end'])}</p>
<dl class="facts"><div class="fact"><dt>Gameplay</dt><dd>{format_time(game['duration'])}</dd></div>
<div class="fact"><dt>Champion select</dt><dd>{format_time(game['champ_select_start']) if game['champ_select_start'] is not None else 'Unknown'}</dd></div>
<div class="fact"><dt>Loading</dt><dd>{format_time(game['loading_start']) if game['loading_start'] is not None else 'Unknown'}</dd></div></dl>{image}</article>"""
        )
    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Milloclips scan</title><style>{CSS}</style></head>
<body><header><p class="label">VOD scan</p><h1>Detected games</h1><p class="dek">{html.escape(str(manifest['metadata'].get('title') or manifest['vod_id']))} · {format_time(total)} · {len(games)} games</p><div class="timeline" aria-label="Stream timeline">{''.join(slices)}</div><div class="legend"><span><i class="IN_GAME"></i>Gameplay</span><span><i class="CHAMP_SELECT"></i>Champion select</span><span><i class="LOADING"></i>Loading</span><span><i class="POST_GAME"></i>Post-game</span><span><i class="OTHER"></i>Other</span></div></header>
<main><div class="grid">{''.join(cards) if cards else '<div class="empty">No games detected. Review stream-states.json before continuing.</div>'}</div></main></body></html>"""
    output = vod_dir / "reports" / "scan-report.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output


def shorts_report(vod_dir: Path) -> Path:
    candidates = short_candidates(vod_dir)
    manifest = read_json(vod_dir / "manifest.json")
    source = Path(manifest["source"])
    source_stat = source.stat()
    layout_path = vod_dir / "layout.reviewed.json" if (vod_dir / "layout.reviewed.json").exists() else vod_dir / "layout.json"
    layout = read_json(layout_path)
    diagnostics_path = vod_dir / "short-diagnostics.json"
    diagnostics = read_json(diagnostics_path) if diagnostics_path.exists() else {}
    cards = []
    for index, item in enumerate(candidates, 1):
        render_name = item.get("render_name") or f"short-{item.get('output_number', index):02d}"
        sidecar_path = vod_dir / "renders" / f"{render_name}.json"
        render_path = vod_dir / "renders" / f"{render_name}.mp4"
        captions_path = vod_dir / f"{render_name}.ass"
        specific_review = vod_dir / f"{render_name}.captions.reviewed.json"
        caption_review = specific_review if specific_review.exists() else vod_dir / "short-captions.reviewed.json"
        sidecar = read_json(sidecar_path) if sidecar_path.exists() else {}
        expected_duration = sum(segment["source_end"] - segment["source_start"] for segment in item.get("segments") or [item])
        valid_render = render_path.exists() and sidecar.get("render_version") == 1 \
            and sidecar.get("output") == _media_fingerprint(render_path) and sidecar.get("candidate") == _fingerprint(item) \
            and sidecar.get("captions") == _file_fingerprint(captions_path) and sidecar.get("layout") == _fingerprint(layout) \
            and sidecar.get("caption_review") == _file_fingerprint(caption_review) \
            and sidecar.get("source") == {"path": str(source.resolve()), "size": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns} \
            and abs(float(sidecar.get("expected_duration", -1)) - expected_duration) <= 0.01 \
            and abs(float(sidecar.get("duration", -1)) - expected_duration) <= 0.5 \
            and sidecar.get("width") == 1080 and sidecar.get("height") == 1920 and sidecar.get("audio") is True
        images = [Path(path).resolve() for path in item.get("evidence_images", []) if Path(path).exists()]
        evidence_html = '<div class="evidence">' + "".join(
            f'<img src="{image.as_uri()}" alt="Hook or payoff evidence">' for image in images
        ) + "</div>" if images else ""
        cards.append(
            f"""<article class="story"><div class="story-head"><span class="rank">{index:02d}</span><div class="story-content"><h2>{html.escape(item['title'])}</h2><p class="time">{format_time(item['source_start'])} → {format_time(item['source_end'])} · {format_time(item.get('duration', item['source_end'] - item['source_start']))} edited · {len(item.get('segments', [item]))} scene(s) · {html.escape(render_name)}.mp4</p><span class="status{' valid' if valid_render else ''}">{'validated render' if valid_render else 'render missing or stale'}</span></div></div>
<dl class="facts"><div class="fact"><dt>Hook</dt><dd>{html.escape(item.get('hook', ''))}</dd></div><div class="fact"><dt>Payoff</dt><dd>{html.escape(item.get('payoff', ''))}</dd></div><div class="fact"><dt>Class</dt><dd>{html.escape(item.get('quality_class', 'unclassified'))}</dd></div><div class="fact"><dt>Gate</dt><dd>{html.escape(item.get('gate_reason', ''))}</dd></div></dl>
<p class="segments">Source cuts: <span>{' · '.join(f"{format_time(segment['source_start'])}–{format_time(segment['source_end'])}" for segment in item.get('segments', [item]))}</span></p>{evidence_html}</article>"""
        )
    rejected = "".join(
        f'<article class="story"><p class="eyebrow">{html.escape(str(item.get("stage", "gate")))}</p><h2>{html.escape(str(item.get("title", "Untitled")))}</h2><p class="summary">{html.escape(str(item.get("reason", "Rejected")))}</p></article>'
        for item in diagnostics.get("rejected", [])
    )
    summary = (f"{diagnostics.get('proposal_count', 0)} proposals · {diagnostics.get('verified_count', 0)} verified · "
               f"{diagnostics.get('gated_count', 0)} passed gates · {len(candidates)} selected")
    rejected_section = f'<h1 style="margin:48px 0 18px">Rejected</h1><div class="stories">{rejected}</div>' if rejected else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Milloclips shorts</title><style>{CSS}</style></head><body><header><p class="label">Shorts diagnostics</p><h1>Selected stories</h1><p class="dek">{summary}. Times refer to the source VOD; edited length excludes gaps between scenes.</p></header><main><div class="stories">{''.join(cards) if cards else '<div class="empty">No candidate cleared the automatic quality gates.</div>'}</div>{rejected_section}</main></body></html>"""
    output = vod_dir / "reports" / "short-candidates.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return output
