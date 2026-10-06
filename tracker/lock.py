"""Lock file next to the database, to warn about use on another machine (e.g. OneDrive sync lag)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STALE_AFTER = timedelta(hours=2)


def read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def foreign_lock(path: Path, machine: str, now: datetime | None = None) -> dict | None:
    """Return the lock if it belongs to another machine and is under 2 hours old."""
    data = read_lock(path)
    if not data:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(data["time"])
    except (KeyError, ValueError):
        return None
    if data.get("machine") != machine and now - ts < STALE_AFTER:
        return data
    return None


def write_lock(path: Path, machine: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"machine": machine, "time": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def release_lock(path: Path, machine: str) -> None:
    data = read_lock(path)
    if data and data.get("machine") == machine:
        try:
            path.unlink()
        except OSError:
            pass
