"""Static, self-contained HTML run report (inline CSS, no JS) from results.json (+ optional events.jsonl).

    uv run python -m agent.html_report results/results.json [--events runs/<id>/events.jsonl] -o results/report.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1f21;--muted:#6a6f76;--card:#fff;--line:#e3e4e6;--code:#f3f4f5;--badge-fg:#fff;
--routed:#1f7a4d;--review:#a15c00;--dup:#3d5a99;--already:#5f6670;--failed:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e6e7e9;--muted:#9aa0a8;--card:#1c1e21;
--line:#2c2f33;--code:#24272b;--badge-fg:#111;--routed:#4fc28a;--review:#e6a23c;--dup:#8fa8e6;--already:#a3a9b1;--failed:#f07167}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--muted);margin-bottom:16px}.tiles{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:110px}
.tile b{display:block;font-size:20px}.tile span{color:var(--muted);font-size:12px}
table.sum{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;
overflow:hidden;margin-bottom:24px}.sum th,.sum td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);
vertical-align:top}.sum td:nth-child(1),.sum td:nth-child(4){white-space:nowrap}.sum th{font-size:12px;color:var(--muted);font-weight:600}.wrap{overflow-x:auto}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:var(--badge-fg);white-space:nowrap}
.routed{background:var(--routed)}.needs_review{background:var(--review)}.skipped_duplicate{background:var(--dup)}
.already_routed{background:var(--already)}.failed{background:var(--failed)}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
summary .id{font-weight:700;font-family:ui-monospace,Menlo,monospace}.body{padding:0 12px 12px}
.grid{display:grid;grid-template-columns:150px 1fr;gap:4px 12px}.grid dt{color:var(--muted)}.grid dd{margin:0}
code,.q{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--code);border-radius:4px;padding:1px 4px}
ul{margin:2px 0;padding-left:18px}.tl{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.tl td{padding:1px 8px 1px 0;vertical-align:top}.muted{color:var(--muted)}a{color:inherit}
@media (max-width:640px){.grid{grid-template-columns:1fr}.grid dt{margin-top:6px}}
"""


def _e(x) -> str:
    return escape("" if x is None else str(x))


def badge(action: str) -> str:
    return f'<span class="badge {_e(action)}">{_e(action)}</span>'


def _list(items) -> str:
    items = [i for i in (items or []) if i]
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else '<span class="muted">none</span>'


def _quotes(items) -> str:
    items = [i for i in (items or []) if i]
    return " ".join(f'<span class="q">&ldquo;{_e(i)}&rdquo;</span>' for i in items) or '<span class="muted">none</span>'


def _route(sel) -> str:
    return " / ".join(_e(sel.get(k)) for k in ("assigned_to", "doc_type", "folder")) if sel else "-"


def load_events(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def find_events(results_path: Path, run_id: str | None) -> Path | None:
    cands = [results_path.parent / "events.jsonl"]
    if run_id:
        cands.append(results_path.resolve().parent.parent / "runs" / run_id / "events.jsonl")
    return next((p for p in cands if p.exists()), None)


def find_traces(events_path: Path | None, out_dir: Path) -> dict[str, list[str]]:
    """Trace zips live next to events.jsonl in traces/<label>-<fax>-<ms>.zip; return links relative to the report."""
    out: dict[str, list[str]] = {}
    if not events_path:
        return out
    tdir = events_path.parent / "traces"
    for z in sorted(tdir.glob("*.zip")) if tdir.is_dir() else []:
        parts = z.stem.split("-")
        fid = "-".join(parts[1:-1]) if len(parts) >= 3 else None
        if fid:
            out.setdefault(fid, []).append(os.path.relpath(z.resolve(), out_dir.resolve()))
    return out


def timeline(evs: list[dict]) -> str:
    if not evs:
        return '<span class="muted">no events</span>'
    t0 = evs[0]["ts"]
    rows = []
    skip = {"ts", "run", "event", "fax_id"}
    for e in evs:
        extra = ", ".join(f"{k}={v}" for k, v in e.items() if k not in skip and not isinstance(v, (dict, list)))
        rows.append(f"<tr><td>+{e['ts'] - t0:6.2f}s</td><td>{_e(e['event'])}</td>"
                    f"<td class='muted'>{_e(extra[:160])}</td></tr>")
    return f'<table class="tl">{"".join(rows)}</table>'


def fax_card(r: dict, evs: list[dict], traces: list[str]) -> str:
    ver = r.get("verification") or {}
    ex = r.get("extraction") or {}
    attempts = r.get("attempts") or []
    att = "".join(f"<li><code>{_e(a.get('stage'))}</code> &rarr; <b>{_e(a.get('outcome'))}</b> "
                  f"<span class='muted'>{_e((a.get('detail') or '')[:160])}</span></li>" for a in attempts)
    dup = f" of <b>{_e(r['duplicate_of'])}</b>" if r.get("duplicate_of") and r["action"] == "skipped_duplicate" else ""
    cost = f"${ex['cost_usd']:.4f}" if ex.get("cost_usd") is not None else "-"
    cc = ex.get("crosscheck") or {}
    cc_txt = ("not run (Tesseract not installed)" if not cc.get("ran") else
              f"ran, mean conf {cc.get('mean_conf')}, {len(cc.get('contradictions') or [])} contradiction(s)")
    trace_html = " ".join(f'<a href="{_e(t)}">{_e(Path(t).name)}</a>' for t in traces) or '<span class="muted">none</span>'
    note = r.get("note_sent")
    return f"""<details>
<summary><span class="id">{_e(r['fax_id'])}</span>{badge(r['action'])}{dup}
<span>{_e(r.get('extracted_type') or 'unclassified')}</span><span class="muted">{_e(r.get('source_doc_id') or '')}</span>
<span class="muted">verification: {_e(ver.get('status'))} &middot; retries: {_e(r.get('retries'))}</span></summary>
<div class="body"><dl class="grid">
<dt>Received / sender</dt><dd>{_e(r.get('received'))} &middot; {_e(r.get('sender'))} &middot; {_e(r.get('pages'))} page(s)</dd>
<dt>Decision reasons</dt><dd>{_list(r.get('reasons'))}</dd>
<dt>Explicit flags</dt><dd>{_quotes(r.get('flags'))}</dd>
<dt>Evidence</dt><dd>{_quotes(r.get('evidence'))}</dd>
<dt>Selected fields</dt><dd>{_route(r.get('selected_fields'))}</dd>
<dt>Note sent</dt><dd>{_e(note) if note else '<span class="muted">none</span>'}
{' <span class="muted">(recorded; the portal never renders notes, so not UI-verifiable)</span>' if note else ''}</dd>
<dt>Verification</dt><dd><b>{_e(ver.get('status'))}</b> <span class="muted">{_e(ver.get('detail') or '')}</span></dd>
<dt>Sessions</dt><dd>A: <code>{_e(', '.join((r.get('sessions') or {}).get('download_session_a') or []))}</code>
 &middot; B: <code>{_e(', '.join((r.get('sessions') or {}).get('route_sessions_b') or []) or '-')}</code></dd>
<dt>Extraction</dt><dd>{_e(ex.get('engine'))} &middot; {_e(ex.get('model'))} &middot; cache hit: {_e(ex.get('cache_hit'))}
 &middot; {_e(ex.get('input_tokens'))}/{_e(ex.get('output_tokens'))} tok &middot; {cost}
 &middot; Tesseract cross-check: {_e(cc_txt)}{(' &middot; error: ' + _e(ex['error'])) if ex.get('error') else ''}</dd>
<dt>Errors / recovery</dt><dd>{_list(r.get('errors'))}</dd>
<dt>Attempts</dt><dd>{('<ul>' + att + '</ul>') if att else '<span class="muted">none</span>'}</dd>
<dt>Timeline</dt><dd>{timeline(evs)}</dd>
<dt>Traces</dt><dd>{trace_html}</dd>
</dl></div></details>"""


def render(doc: dict, events: list[dict], traces: dict[str, list[str]]) -> str:
    results = doc.get("results") or []
    counts = Counter(r["action"] for r in results)
    per: dict[str, list[dict]] = {}
    for e in events:
        if e.get("fax_id"):
            per.setdefault(e["fax_id"], []).append(e)
    total_cost = sum((r.get("extraction") or {}).get("cost_usd") or 0 for r in results)
    verified = sum((r.get("verification") or {}).get("status") in ("ui_verified", "ui_status_new",
                                                                    "preexisting_route_observed") for r in results)
    run_ts = [e["ts"] for e in events if e["event"] in ("run_started", "run_finished")]
    wall = f"{run_ts[-1] - run_ts[0]:.1f} s" if len(run_ts) >= 2 else "-"
    mode = next((e.get("mode") for e in events if e["event"] == "run_started"), None)
    tiles = [(len(results), "faxes"), *((counts.get(a, 0), a) for a in
             ("routed", "needs_review", "skipped_duplicate", "already_routed", "failed")),
             (f"{verified}/{len(results)}", "UI-verified"), (f"${total_cost:.2f}", "vision cost (live)"), (wall, "wall clock")]
    tile_html = "".join(f'<div class="tile"><b>{_e(v)}</b><span>{_e(k)}</span></div>' for v, k in tiles)
    rows = "".join(
        f"<tr><td><a href='#{_e(r['fax_id'])}'>{_e(r['fax_id'])}</a></td><td>{badge(r['action'])}</td>"
        f"<td>{_e(r.get('extracted_type') or '-')}</td><td>{_e(r.get('source_doc_id') or '-')}</td>"
        f"<td>{_e(r.get('duplicate_of') or '')}</td><td>{_route(r.get('selected_fields'))}</td>"
        f"<td>{_e((r.get('verification') or {}).get('status'))}</td><td>{_e(r.get('retries'))}</td></tr>"
        for r in results)
    cards = "".join(f'<div id="{_e(r["fax_id"])}">{fax_card(r, per.get(r["fax_id"], []), traces.get(r["fax_id"], []))}</div>'
                    for r in results)
    gen = datetime.fromtimestamp(doc.get("generated_at") or 0, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Linda Run Report</title>
<style>{CSS}</style></head><body><main>
<h1>Linda Mini fax routing: run report</h1>
<div class="meta">run <code>{_e(doc.get('run_id'))}</code> &middot; mode {_e(mode or 'unknown')} &middot; generated {gen}
 &middot; synthetic data only</div>
<div class="tiles">{tile_html}</div>
<div class="wrap"><table class="sum"><thead><tr><th>Fax</th><th>Action</th><th>Type</th><th>Source ID</th><th>Dup of</th>
<th>Selected route</th><th>Verification</th><th>Retries</th></tr></thead><tbody>{rows}</tbody></table></div>
{cards}
</main></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent.html_report", description=__doc__.splitlines()[0])
    ap.add_argument("results", type=Path)
    ap.add_argument("--events", type=Path, default=None, help="default: events.jsonl next to results, or runs/<run_id>/")
    ap.add_argument("-o", "--out", type=Path, default=Path("results/report.html"))
    args = ap.parse_args(argv)
    doc = json.loads(args.results.read_text())
    ev_path = args.events or find_events(args.results, doc.get("run_id"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    html = render(doc, load_events(ev_path), find_traces(ev_path, args.out.parent))
    args.out.write_text(html)
    print(f"wrote {args.out} ({len(doc.get('results') or [])} faxes, events: {ev_path or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
