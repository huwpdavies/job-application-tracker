"""Configuration from .env (and optional .env.local per-machine overrides)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_data_dir

PROJECT_DIR = Path(__file__).resolve().parent.parent
LONDON = "Europe/London"

# Read-only scopes. "offline_access" is added by MSAL itself (it rejects it if passed).
SCOPES = ["User.Read", "Mail.Read", "Calendars.Read"]
AUTHORITY = "https://login.microsoftonline.com/consumers"


@dataclass(frozen=True)
class Config:
    client_id: str
    anthropic_api_key: str
    db_path: Path
    claude_model: str
    confidence_threshold: float
    follow_up_days: int
    applications_folder: str
    host: str
    port: int
    token_cache_path: Path
    machine_name: str

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".lock")


def token_cache_dir() -> Path:
    """Per-machine location, deliberately outside the (possibly OneDrive-synced) project and DB folders."""
    d = Path(user_data_dir("JobApplicationTracker", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> Config:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)
    raw_db = os.environ.get("DB_PATH", "./data/tracker.db")
    db_path = Path(os.path.expandvars(raw_db)).expanduser()
    if not db_path.is_absolute():
        db_path = (PROJECT_DIR / db_path).resolve()
    return Config(
        client_id=os.environ.get("AZURE_CLIENT_ID", "").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        db_path=db_path,
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5"),
        confidence_threshold=float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7")),
        follow_up_days=int(os.environ.get("FOLLOW_UP_DAYS", "14")),
        applications_folder=os.environ.get("APPLICATIONS_FOLDER", "Job Applications"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        token_cache_path=token_cache_dir() / "token_cache.json",
        machine_name=socket.gethostname(),
    )
