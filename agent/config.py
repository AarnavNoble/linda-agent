"""Runtime configuration. Everything is overridable via environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    base_url: str = os.getenv("LINDA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    username: str = os.getenv("LINDA_USERNAME", "agent.demo")
    password: str = os.getenv("LINDA_PASSWORD", "demo-password-123")
    ocr_mode: str = os.getenv("LINDA_OCR_MODE", "vision")  # vision | replay
    vision_model: str = os.getenv("LINDA_VISION_MODEL", "claude-opus-5-5")
    headed: bool = os.getenv("LINDA_HEADED", "0") == "1"
    work_dir: Path = Path(os.getenv("LINDA_WORK_DIR", str(ROOT)))
    cache_dir: Path = Path(os.getenv("LINDA_CACHE_DIR", str(ROOT / "cache")))
    # Test hook for the kill/resume chaos test: hard-exit at a named checkpoint.
    crash_at: str = os.getenv("LINDA_AGENT_CRASH_AT", "")
    # Timeouts (ms)
    nav_timeout_ms: int = int(os.getenv("LINDA_NAV_TIMEOUT_MS", "15000"))
    download_wait_ms: int = int(os.getenv("LINDA_DOWNLOAD_WAIT_MS", "10000"))
    submit_timeout_ms: int = int(os.getenv("LINDA_SUBMIT_TIMEOUT_MS", "20000"))
    max_download_attempts: int = 3
    max_route_attempts: int = 3

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"

    @property
    def download_dir(self) -> Path:
        return self.work_dir / "downloads"

    @property
    def results_dir(self) -> Path:
        return self.work_dir / "results"

    @property
    def artifacts_dir(self) -> Path:
        return self.work_dir / "runs"


def load() -> Config:
    return Config()
