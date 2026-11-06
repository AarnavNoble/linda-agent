"""Durable checkpoint store (SQLite, WAL) + single-run file lock.

Per-fax state machine:
  DISCOVERED -> DOWNLOADED -> EXTRACTED -> DECIDED -> INTENT -> SUBMITTED_UNVERIFIED -> DONE
Invariant: any fax at INTENT or later must have its portal state re-read from the UI
before any further action (we may have crashed after clicking OK).
"""
from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

STATES = ("DISCOVERED", "DOWNLOADED", "EXTRACTED", "DECIDED", "INTENT", "SUBMITTED_UNVERIFIED", "DONE")
RANK = {s: i for i, s in enumerate(STATES)}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS faxes (
  fax_id TEXT PRIMARY KEY,
  received TEXT, sender TEXT, pages INTEGER,
  ui_status_at_discovery TEXT,
  state TEXT NOT NULL,
  pdf_path TEXT, sha256 TEXT, download_session TEXT,
  extraction TEXT, decision TEXT, intent TEXT, result TEXT,
  reported INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fax_id TEXT NOT NULL, run_id TEXT NOT NULL, stage TEXT NOT NULL,
  outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL, status TEXT
);
"""

_JSON_COLS = ("extraction", "decision", "intent", "result")


class LockHeld(RuntimeError):
    pass


@contextmanager
def run_lock(state_dir: Path):
    """Exclusive process lock so two workers never act on the same checkpoint."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = (state_dir / "agent.lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise LockHeld("another agent run holds state/agent.lock") from e
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # checkpoint must survive kill -9
        self.db.executescript(_SCHEMA)
        # Additive migrations for checkpoints written by older versions (resume must not break).
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(faxes)")}
        if "download_session" not in cols:
            self.db.execute("ALTER TABLE faxes ADD COLUMN download_session TEXT")

    # ---- faxes -------------------------------------------------------------
    def get(self, fax_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM faxes WHERE fax_id=?", (fax_id,)).fetchone()
        return self._decode(row) if row else None

    def all(self) -> list[dict]:
        return [self._decode(r) for r in self.db.execute("SELECT * FROM faxes ORDER BY fax_id")]

    def upsert_discovered(self, fax_id, received, sender, pages, ui_status) -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO faxes(fax_id,received,sender,pages,ui_status_at_discovery,state,updated_at)
               VALUES(?,?,?,?,?, 'DISCOVERED', ?)
               ON CONFLICT(fax_id) DO UPDATE SET received=excluded.received, sender=excluded.sender,
                 pages=excluded.pages, updated_at=excluded.updated_at""",
            (fax_id, received, sender, pages, ui_status, now),
        )

    def update(self, fax_id: str, **fields) -> None:
        for k in _JSON_COLS:
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], default=str)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE faxes SET {cols} WHERE fax_id=?", (*fields.values(), fax_id))

    def advance(self, fax_id: str, state: str, **fields) -> None:
        assert state in RANK, state
        self.update(fax_id, state=state, **fields)

    # ---- attempts ----------------------------------------------------------
    def attempt(self, fax_id: str, run_id: str, stage: str, outcome: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO attempts(fax_id,run_id,stage,outcome,detail,at) VALUES(?,?,?,?,?,?)",
            (fax_id, run_id, stage, outcome, detail[:2000], time.time()),
        )

    def attempts(self, fax_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT run_id,stage,outcome,detail,at FROM attempts WHERE fax_id=? ORDER BY id", (fax_id,))]

    # ---- runs --------------------------------------------------------------
    def start_run(self, run_id: str) -> None:
        self.db.execute("INSERT INTO runs(run_id,started_at,status) VALUES(?,?,'running')", (run_id, time.time()))

    def finish_run(self, run_id: str, status: str) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=? WHERE run_id=?", (time.time(), status, run_id))

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY started_at")]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in _JSON_COLS:
            if d.get(k):
                d[k] = json.loads(d[k])
        return d
