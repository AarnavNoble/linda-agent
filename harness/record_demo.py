"""Record a narrated demo video of one full agent run, end to end, with no API key.

    uv run python -m harness.record_demo [-o results/demo.mp4] [--slow-mo 250]

What it does (evaluator tooling, like harness/chaos.py; the worker never imports it):
  1. starts a throwaway portal (own port, own LINDA_DB_PATH, seed state) from the supplied linda-mini/;
  2. runs the real agent (`python -m agent run --replay`) against it with LINDA_RECORD_VIDEO_DIR set, so every
     browser session the agent opens (Session A: discover + download; one fresh Session B per routed fax)
     records its own video;
  3. films the portal's Routed Documents queue afterwards, as a human would check it;
  4. stitches title cards, the session videos in the order they were opened, the queue check and a results
     card into one MP4 with ffmpeg.

Needs ffmpeg on PATH. Takes about 2-3 minutes; browser windows open on screen unless --headless.
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
W, H, FPS = 1280, 800, 25

CARD_CSS = """body{margin:0;width:1280px;height:800px;display:flex;flex-direction:column;justify-content:center;
padding:0 96px;box-sizing:border-box;background:#0f1b2d;color:#e8eef6;font:22px/1.5 -apple-system,Helvetica,Arial}
h1{font-size:46px;margin:0 0 18px;color:#fff}h2{font-size:22px;color:#8fb3de;margin:0 0 28px;font-weight:600}
li{margin:6px 0}table{border-collapse:collapse;font-size:17px}td,th{padding:5px 14px;text-align:left;
border-bottom:1px solid #2a3b55}th{color:#8fb3de}.ok{color:#7ee2a8}.rv{color:#ffd27a}.sk{color:#9fb0c6}"""


def card(page, path: Path, title: str, sub: str = "", body: str = "") -> Path:
    page.set_content(f"<html><head><style>{CARD_CSS}</style></head><body><h1>{html.escape(title)}</h1>"
                     f"<h2>{html.escape(sub)}</h2>{body}</body></html>")
    page.screenshot(path=str(path))
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
    page.wait_for_timeout(2500)
    video = page.video
    ctx.close()
    return Path(video.path())


def to_clip(src: Path, dst: Path, still_s: float | None = None, max_s: float | None = None) -> Path:
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={FPS},format=yuv420p")
    pre = ["-loop", "1", "-t", str(still_s)] if still_s else []
    cut = ["-t", str(max_s)] if max_s else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *pre, "-i", str(src), *cut, "-vf", vf, "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(dst)], check=True)
    return dst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.record_demo", description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", type=Path, default=ROOT / "results" / "demo.mp4")
    ap.add_argument("--slow-mo", type=int, default=700, help="ms between browser actions, so clicks are visible")
    ap.add_argument("--show", default="FX-201,FX-202",
                    help="faxes whose Session B is shown in full (the rest are summarised on a card)")
    ap.add_argument("--a-seconds", type=float, default=35, help="how much of Session A (downloads) to show")
    ap.add_argument("--headless", action="store_true",
                    help="no visible windows (faster, but Chromium headless shows the PDF preview pane blank)")
    ap.add_argument("--keep", action="store_true", help="keep the temp work dir (videos, portal log)")
    a = ap.parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found (brew install ffmpeg)", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="linda-demo-"))
    vids, clips = tmp / "videos", tmp / "clips"
    vids.mkdir()
    clips.mkdir()
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
        sessions = sorted(vids.glob("*.webm"), key=lambda p: p.name)
        print(f"[demo] agent exit {rc}; {len(sessions)} session videos; filming Routed Documents ...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=a.headless)
            queue = film_queue(browser, portal.url, tmp)
            page = browser.new_page(viewport={"width": W, "height": H})
            n_routed = sum(r["action"] in ("routed", "needs_review") for r in results)
            show = [f.strip() for f in a.show.split(",") if f.strip()]
            by_id = {r["fax_id"]: r for r in results}

            def fax_card(fid: str, n: int) -> Path:
                r = by_id[fid]
                sel = r.get("selected_fields") or {}
                why = "; ".join((r.get("reasons") or [])[:1]) or "matches the SOP table"
                return card(page, tmp / f"f{n}.png", f"{fid}: {r.get('extracted_type') or 'unreadable'} → {r['action']}",
                            f"{sel.get('assigned_to', '-')} · {sel.get('doc_type', '-')} · {sel.get('folder', '-')}",
                            f"<p>Why: {html.escape(why[:220])}</p><p style='color:#9fb0c6'>Fresh login (the portal "
                            "requires routing from a different session than the download) → open the fax → confirm "
                            "it is still New → choose the three fields by label → Review Routing → check the summary "
                            "→ OK → re-read the fax and Routed Documents.</p>")

            cards = {
                "intro": card(page, tmp / "c0.png", "Linda Mini fax-routing agent",
                              "Playwright worker · Claude vision (replayed from cache) · UI-verified routing",
                              "<ul><li>Fresh portal, seed state: 12 inbound faxes, one already routed</li>"
                              "<li>Session A logs in, pages the inbox, downloads every fax PDF</li>"
                              "<li>Every page is read; a pure, tested decider applies the SOP</li>"
                              "<li>Each route is submitted from its own fresh login (Session B) and re-read in "
                              "the UI</li></ul>"),
                "A": card(page, tmp / "c1.png", "Session A: find and download",
                          "Log in → page through the inbox → open each fax → Download Fax PDF"),
                "rest": card(page, tmp / "c2.png", "The other faxes go the same way",
                             f"{len(results) - len(show)} more: each routed or sent to review from its own fresh "
                             "login, or deliberately left alone (duplicate, already routed)"),
                "Q": card(page, tmp / "c3.png", "Check the result in the portal", "Routed Documents work queue"),
                "R": card(page, tmp / "c4.png", f"Done: {n_routed} submitted, every one verified in the UI",
                          "Duplicate and pre-routed faxes were left untouched", results_body(results)),
                **{fid: fax_card(fid, i) for i, fid in enumerate(show) if fid in by_id},
            }
            browser.close()

        a_vid = next((v for v in sessions if v.name.endswith("-A-inbox.webm")), None)
        seq: list[Path] = [to_clip(cards["intro"], clips / "000.mp4", 7), to_clip(cards["A"], clips / "001.mp4", 4)]
        if a_vid:
            seq.append(to_clip(a_vid, clips / "002.mp4", max_s=a.a_seconds))
        for i, fid in enumerate(show):
            v = next((v for v in sessions if v.name.endswith(f"-B-{fid}.webm")), None)
            if v and fid in cards:
                seq += [to_clip(cards[fid], clips / f"1{i:02d}c.mp4", 6), to_clip(v, clips / f"1{i:02d}.mp4")]
        seq += [to_clip(cards["rest"], clips / "800.mp4", 5), to_clip(cards["Q"], clips / "900.mp4", 3),
                to_clip(queue, clips / "901.mp4"), to_clip(cards["R"], clips / "902.mp4", 10)]
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{c}'\n" for c in seq))
        a.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(a.out)], check=True)
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(a.out)], capture_output=True, text=True).stdout.strip()
        print(f"[demo] wrote {a.out} ({float(dur or 0):.0f}s, {a.out.stat().st_size / 1e6:.1f} MB)")
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
