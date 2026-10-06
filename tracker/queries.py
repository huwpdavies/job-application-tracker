"""Read-side queries and small rules shared by the web UI (and later the CLI)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .statuses import STATUSES
from .timeutil import parse_dt

SORTABLE = {
    "company": "company COLLATE NOCASE",
    "role": "role_title COLLATE NOCASE",
    "source": "source COLLATE NOCASE",
    "applied": "applied_at",
    "status": "status",
    "activity": "last_activity_at",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days_since(iso: str | None, now: datetime | None = None) -> int | None:
    if not iso:
        return None
    return ((now or now_utc()) - parse_dt(iso, timezone.utc)).days


def needs_followup(app: sqlite3.Row, follow_up_days: int, now: datetime | None = None) -> bool:
    """Applied, no new inbound email for N days, and not snoozed."""
    now = now or now_utc()
    if app["status"] != "Applied":
        return False
    if app["snoozed_until"] and parse_dt(app["snoozed_until"], timezone.utc) > now:
        return False
    ref = app["last_inbound_at"] or app["applied_at"] or app["created_at"]
    d = days_since(ref, now)
    return d is not None and d >= follow_up_days


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status"):
        counts[r["status"]] = r["n"]
    return counts


def review_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM review_items WHERE state='open'").fetchone()[0]


def list_applications(
    conn: sqlite3.Connection, follow_up_days: int, q: str = "", status: str = "", sort: str = "activity",
    direction: str = "desc", followup_only: bool = False,
) -> list[dict]:
    where, args = [], []
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(company LIKE ? OR role_title LIKE ? OR source LIKE ? OR location LIKE ? OR notes LIKE ?)")
        args += [like] * 5
    if status in STATUSES:
        where.append("status = ?")
        args.append(status)
    col = SORTABLE.get(sort, SORTABLE["activity"])
    d = "ASC" if direction == "asc" else "DESC"
    sql = "SELECT * FROM applications" + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY ({col.split()[0]} IS NULL), {col} {d}, id DESC"
    now = now_utc()
    out = []
    for r in conn.execute(sql, args):
        d_act = days_since(r["last_activity_at"] or r["applied_at"] or r["created_at"], now)
        item = dict(r)
        item["days_since"] = d_act
        item["followup"] = needs_followup(r, follow_up_days, now)
        if followup_only and not item["followup"]:
            continue
        out.append(item)
    return out


def upcoming_interviews(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    now = now_utc()
    return conn.execute(
        """SELECT i.*, a.company, a.role_title FROM interviews i JOIN applications a ON a.id=i.application_id
           WHERE i.status='Scheduled' AND i.start_at >= ? AND i.start_at <= ? ORDER BY i.start_at""",
        (now.isoformat(timespec="seconds"), (now + timedelta(days=days)).isoformat(timespec="seconds")),
    ).fetchall()


def followup_list(conn: sqlite3.Connection, follow_up_days: int) -> list[dict]:
    items = list_applications(conn, follow_up_days, followup_only=True, sort="activity", direction="asc")
    return items
