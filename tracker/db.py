"""SQLite access. Journal mode is DELETE (not WAL) because the file may live in a OneDrive folder.

The connection is in autocommit mode; use `tx(conn)` for atomic changes (or `tx(conn, rollback=True)` for dry runs).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}). Update the app on this machine."
        )
    if 0 < version < 2:
        _migrate_to_2(conn)
    if 0 < version < 3:
        _migrate_to_3(conn)
    if 0 < version < 4:
        _migrate_to_4(conn)


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """v4: interviews.google_added_at (remembers that you opened Google Calendar for an interview)."""
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE interviews ADD COLUMN google_added_at TEXT")
        conn.execute("PRAGMA user_version=4")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """v3: "Applied on" is the date of the application's first email (not only confirmations)."""
    conn.execute("BEGIN")
    try:
        conn.execute(
            """UPDATE applications SET applied_at = (
                   SELECT MIN(occurred_at) FROM events WHERE application_id=applications.id AND email_id IS NOT NULL)
               WHERE locked_fields NOT LIKE '%"applied_at"%'
                 AND EXISTS (SELECT 1 FROM events WHERE application_id=applications.id AND email_id IS NOT NULL)""")
        conn.execute("PRAGMA user_version=3")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """v2: interviews.kind ('interview' | 'assessment'). Existing assessment emails become assessment items."""
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE interviews ADD COLUMN kind TEXT NOT NULL DEFAULT 'interview'")
        now = utcnow()
        rows = conn.execute(
            """SELECT e.id, e.application_id, e.received_at, e.classification FROM emails e
               WHERE e.category='assessment' AND e.state='processed' AND e.application_id IS NOT NULL
               ORDER BY e.received_at""").fetchall()
        for r in rows:
            if conn.execute("SELECT 1 FROM interviews WHERE email_id=?", (r["id"],)).fetchone():
                continue
            if conn.execute("SELECT 1 FROM interviews WHERE application_id=? AND kind='assessment'", (r["application_id"],)).fetchone():
                continue  # one item per application, as the sync does
            c = jloads(r["classification"], {}) or {}
            conn.execute(
                """INSERT INTO interviews (application_id, email_id, kind, status, format, stage, created_at, updated_at)
                   VALUES (?,?, 'assessment', 'New', ?, ?, ?, ?)""",
                (r["application_id"], r["id"], c.get("interview_format"), c.get("interview_stage"), r["received_at"], now),
            )
        conn.execute("PRAGMA user_version=2")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def tx(conn: sqlite3.Connection, rollback: bool = False):
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if rollback else "COMMIT")


# --- small key/value helpers (settings and sync_state tables) ------------------------------------
def kv_get(conn: sqlite3.Connection, table: str, key: str, default=None):
    row = conn.execute(f"SELECT value FROM {table} WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, table: str, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO {table}(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )


def jloads(s: str | None, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
