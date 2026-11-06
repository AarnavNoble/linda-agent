"""Copy one finished run into results/<name>/ as a committed, machine-independent sample.

    uv run python -m harness.publish_sample <work_dir> results/sample

Copies results.json/.csv/summary.md and that run's events.jsonl, replaces the absolute work-dir
path with `<work_dir>` so no local paths are committed, then renders report.html.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from agent.html_report import main as html_main


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    work, out = Path(argv[0]).resolve(), Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    doc = json.loads((work / "results" / "results.json").read_text())
    events = work / "runs" / doc["run_id"] / "events.jsonl"
    prefixes = sorted({str(work), str(work).replace("/private/var/", "/var/")}, key=len, reverse=True)
    for src, name in [(work / "results" / "results.json", "results.json"),
                      (work / "results" / "results.csv", "results.csv"),
                      (work / "results" / "summary.md", "summary.md"), (events, "events.jsonl")]:
        text = src.read_text()
        for p in prefixes:
            text = text.replace(p, "<work_dir>")
        (out / name).write_text(text)
    traces = work / "runs" / doc["run_id"] / "traces"
    if len(argv) > 2 and argv[2] == "--with-trace" and traces.exists():
        first_b = next((t for t in sorted(traces.glob("B-*.zip"))), None)
        if first_b:
            shutil.copy(first_b, out / "trace-session-B-example.zip")
    html_main([str(out / "results.json"), "--events", str(out / "events.jsonl"), "-o", str(out / "report.html")])
    print(f"published {doc['run_id']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
