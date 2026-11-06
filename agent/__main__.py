"""CLI: python -m agent run|report|status"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from . import config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent", description="Linda Mini fax routing agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="process the whole inbox (resumes from checkpoint automatically)")
    run.add_argument("--headed", action="store_true", help="show the browser")
    run.add_argument("--replay", action="store_true", help="use committed extraction cache only (no API key)")
    run.add_argument("--fresh", action="store_true", help="discard the local checkpoint before running")
    sub.add_parser("status", help="print checkpoint state per fax")
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.cmd == "run":
        cfg = dataclasses.replace(cfg, headed=args.headed or cfg.headed,
                                  ocr_mode="replay" if args.replay else cfg.ocr_mode)
        if args.fresh:
            for f in cfg.state_dir.glob("checkpoint.sqlite3*"):
                f.unlink()
        from .runner import Runner
        return Runner(cfg).run()
    if args.cmd == "status":
        from .state import Store
        for r in Store(cfg.state_dir / "checkpoint.sqlite3").all():
            res = r.get("result") or {}
            print(json.dumps({"fax_id": r["fax_id"], "state": r["state"], "action": res.get("action"),
                              "verification": (res.get("verification") or {}).get("status"),
                              "reported": r["reported"]}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
