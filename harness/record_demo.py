"""Record a short captioned demo video of one full agent run, with no API key.

    uv run python -m harness.record_demo [-o results/demo.mp4] [--show FX-201,FX-202] [--headless]

What it does (evaluator tooling, like harness/chaos.py; the worker never imports it):
  1. starts a throwaway portal (own port, own LINDA_DB_PATH, seed state) from the supplied linda-mini/;
  2. runs the real agent (`python -m agent run --replay`) against it with LINDA_RECORD_VIDEO_DIR set, so every
     browser session the agent opens (Session A: discover + download; one fresh Session B per routed fax)
     records its own video, named by fax;
  3. films the portal's Routed Documents queue afterwards, as a human would check it;
  4. cuts it to about a minute: an intro card, Session A at double speed, the Session B of a few chosen faxes,
     the queue check and a results card, with lower-third captions and crossfades (ffmpeg).

Needs ffmpeg on PATH. Takes about 2-3 minutes; browser windows open on screen unless --headless
(Chromium headless shows the PDF preview pane blank).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from harness.chaos import Portal, cleanup_all

ROOT = Path(__file__).resolve().parent.parent
W, H, FPS, XFADE = 1280, 800, 25, 0.5

CARD_CSS = """body{margin:0;width:1280px;height:800px;display:flex;flex-direction:column;justify-content:center;
padding:0 96px;box-sizing:border-box;background:linear-gradient(135deg,#0f1b2d,#17324f);color:#e8eef6;
font:22px/1.5 -apple-system,Helvetica,Arial}h1{font-size:48px;margin:0 0 14px;color:#fff}
h2{font-size:22px;color:#8fb3de;margin:0 0 26px;font-weight:600}li{margin:6px 0}
table{border-collapse:collapse;font-size:17px}td,th{padding:5px 14px;text-align:left;border-bottom:1px solid #2a3b55}
th{color:#8fb3de}.ok{color:#7ee2a8}.rv{color:#ffd27a}.sk{color:#9fb0c6}"""

CAPTION_CSS = """html,body{margin:0;width:1280px;height:800px;background:transparent}
.bar{position:absolute;left:40px;right:40px;bottom:36px;padding:16px 24px;border-radius:12px;
background:rgba(12,22,38,.88);color:#fff;font:20px/1.4 -apple-system,Helvetica,Arial;box-shadow:0 6px 24px rgba(0,0,0,.3)}
.k{display:inline-block;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#8fb3de;margin-bottom:4px}
b{color:#ffd27a;font-weight:600}"""


def card(page, path: Path, title: str, sub: str = "", body: str = "") -> Path:
    page.set_content(f"<html><head><style>{CARD_CSS}</style></head><body><h1>{html.escape(title)}</h1>"
                     f"<h2>{html.escape(sub)}</h2>{body}</body></html>")
    page.screenshot(path=str(path))
    return path


def caption(page, path: Path, kicker: str, text_html: str) -> Path:
    page.set_content(f"<html><head><style>{CAPTION_CSS}</style></head><body><div class='bar'>"
                     f"<div class='k'>{html.escape(kicker)}</div><div>{text_html}</div></div></body></html>")
    page.screenshot(path=str(path), omit_background=True)
    return path


def results_body(results: list[dict]) -> str:
    cls = {"routed": "ok", "needs_review": "rv"}
    rows = []
    for r in results:
        sel = r.get("selected_fields") or {}
        act = r["action"] + (f" of {r['duplicate_of']}" if r.get("duplicate_of") else "")
        rows.append(f"<tr><td>{r['fax_id']}</td><td>{html.escape(r.get('extracted_type') or '-')}</td>"
                    f"<td class='{cls.get(r['action'], 'sk')}'>{html.escape(act)}</td>"
                    f"<td>{html.escape(sel.get('assigned_to') or '-')}</td>"
                    f"<td>{html.escape((r.get('verification') or {}).get('status') or '-')}</td></tr>")
    return ("<table><tr><th>Fax</th><th>Read as</th><th>Action</th><th>Assigned to</th><th>Verified</th></tr>"
            + "".join(rows) + "</table>")


def film_queue(browser, url: str, out_dir: Path) -> Path:
    """A human-style check of Routed Documents after the run (harness navigation, not the agent's)."""
    size = {"width": W, "height": H}
    ctx = browser.new_context(record_video_dir=str(out_dir), record_video_size=size, viewport=size)
    page = ctx.new_page()
    page.goto(url + "/")
    page.fill("#username", "agent.demo")
    page.fill("#password", "demo-password-123")
    page.click("button[type=submit]")
    page.get_by_role("link", name="Routed Documents").first.click()
    page.wait_for_load_state()
    page.wait_for_timeout(2500)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(2000)
    video = page.video
    ctx.close()
    return Path(video.path())


def clip(src: Path, dst: Path, *, still_s: float | None = None, skip_s: float = 0, max_s: float | None = None,
         speed: float = 1.0, cap: Path | None = None) -> Path:
    """Normalise any source (card PNG or session webm) to a same-format H.264 clip, optionally sped up,
    trimmed and with a caption overlaid."""
    norm = (f"setpts=PTS/{speed},scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white,fps={FPS}")
    inp = (["-loop", "1", "-t", str(still_s)] if still_s else ["-ss", str(skip_s)]) + ["-i", str(src)]
    if cap:
        inp += ["-loop", "1", "-i", str(cap)]
        graph = f"[0:v]{norm}[v];[v][1:v]overlay=0:0:shortest=1,format=yuv420p[o]"
    else:
        graph = f"[0:v]{norm},format=yuv420p[o]"
    out = ["-t", str(max_s)] if max_s else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inp, "-filter_complex", graph, "-map", "[o]", *out,
                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", str(dst)], check=True)
    return dst


def duration(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(p)], capture_output=True, text=True, check=True).stdout.strip())


def crossfade(clips: list[Path], out: Path) -> None:
    """Join clips with a short crossfade between each (xfade chain)."""
    args, graph, prev, t = [], [], "[0:v]", 0.0
    for c in clips:
        args += ["-i", str(c)]
    for i in range(1, len(clips)):
        t += duration(clips[i - 1]) - XFADE
        tag = f"[x{i}]"
        graph.append(f"{prev}[{i}:v]xfade=transition=fade:duration={XFADE}:offset={t:.3f}{tag}")
        prev = tag
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args, "-filter_complex", ";".join(graph),
                    "-map", prev, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(out)], check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.record_demo", description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", type=Path, default=ROOT / "results" / "demo.mp4")
    ap.add_argument("--slow-mo", type=int, default=500, help="ms between browser actions, so clicks are visible")
    ap.add_argument("--show", default="FX-201,FX-202", help="faxes whose routing session is shown")
    ap.add_argument("--a-seconds", type=float, default=12, help="seconds of Session A shown (played at 2x)")
    ap.add_argument("--headless", action="store_true", help="no visible windows (PDF preview pane renders blank)")
    ap.add_argument("--keep", action="store_true", help="keep the temp work dir (videos, portal log)")
    a = ap.parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found (brew install ffmpeg)", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="linda-demo-"))
    vids, work = tmp / "videos", tmp / "clips"
    vids.mkdir()
    work.mkdir()
    portal = Portal("none", tmp).start()
    try:
        print(f"[demo] portal {portal.url} (fresh seed DB); recording agent run ...")
        env = {**os.environ, "LINDA_BASE_URL": portal.url, "LINDA_WORK_DIR": str(tmp / "work"),
               "LINDA_OCR_MODE": "replay", "LINDA_CACHE_DIR": str(ROOT / "cache"),
               "LINDA_RECORD_VIDEO_DIR": str(vids), "LINDA_SLOW_MO_MS": str(a.slow_mo),
               "LINDA_HEADED": "0" if a.headless else "1"}
        env.pop("ANTHROPIC_API_KEY", None)
        rc = subprocess.run([sys.executable, "-m", "agent", "run", "--replay"], cwd=ROOT, env=env).returncode
        results = json.loads((tmp / "work" / "results" / "results.json").read_text())["results"]
        by_id = {r["fax_id"]: r for r in results}
        sessions = sorted(vids.glob("*.webm"), key=lambda p: p.name)
        show = [f.strip() for f in a.show.split(",") if f.strip() in by_id]
        print(f"[demo] agent exit {rc}; {len(sessions)} session videos; filming Routed Documents ...")

        n_sub = sum(r["action"] in ("routed", "needs_review") for r in results)
        n_rev = sum(r["action"] == "needs_review" for r in results)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=a.headless)
            queue = film_queue(browser, portal.url, tmp)
            pg = browser.new_page(viewport={"width": W, "height": H})
            intro = card(pg, tmp / "intro.png", "Linda Mini fax-routing agent",
                         "12 inbound faxes · read every page · route by SOP · verify in the UI",
                         "<ul><li>Session A logs in and downloads every fax</li>"
                         "<li>Claude vision reads each page; a tested rule engine applies the SOP</li>"
                         "<li>Each fax is routed from its own fresh login, then checked in the portal</li></ul>")
            outro = card(pg, tmp / "outro.png", f"{n_sub} routed, {n_sub} verified in the UI",
                         f"{n_rev} sent to manual review with a reason · duplicate and pre-routed faxes untouched",
                         results_body(results))
            cap_a = caption(pg, tmp / "cap_a.png", "Session A · 2× speed",
                            "Log in, page through the inbox, download each fax PDF through the portal")
            caps = {}
            for fid in show:
                r = by_id[fid]
                sel = r.get("selected_fields") or {}
                why = (r.get("reasons") or [""])[0] if r["action"] == "needs_review" else "clean single document"
                caps[fid] = caption(pg, tmp / f"cap_{fid}.png",
                                    f"Session B · {fid} · fresh login (the portal requires a different session)",
                                    f"Read as <b>{html.escape(r.get('extracted_type') or '-')}</b> "
                                    f"({html.escape(why[:90])}) → <b>{html.escape(sel.get('assigned_to') or '-')}</b>"
                                    f" · then re-read the fax to confirm")
            others = len(results) - len(show)
            cap_q = caption(pg, tmp / "cap_q.png", "Verification",
                            f"The other {others} faxes went the same way. Routed Documents now lists every "
                            "submitted fax; the duplicate (FX-210) was correctly never submitted")
            browser.close()

        seq = [clip(intro, work / "00.mp4", still_s=5)]
        a_vid = next((v for v in sessions if v.name.endswith("-A-inbox.webm")), None)
        if a_vid:
            seq.append(clip(a_vid, work / "01.mp4", skip_s=1, max_s=a.a_seconds, speed=2.0, cap=cap_a))
        for i, fid in enumerate(show):
            v = next((v for v in sessions if v.name.endswith(f"-B-{fid}.webm")), None)
            if v:
                seq.append(clip(v, work / f"1{i}.mp4", skip_s=0.5, speed=1.25, cap=caps[fid]))
        seq += [clip(queue, work / "80.mp4", skip_s=1.5, cap=cap_q), clip(outro, work / "90.mp4", still_s=7)]
        a.out.parent.mkdir(parents=True, exist_ok=True)
        crossfade(seq, a.out)
        print(f"[demo] wrote {a.out} ({duration(a.out):.0f}s, {a.out.stat().st_size / 1e6:.1f} MB)")
        return 0 if rc == 0 else 1
    finally:
        portal.release()
        cleanup_all()
        if a.keep:
            print(f"[demo] kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
