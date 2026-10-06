"""Manual operations on applications and interviews: merge, split, recompute, add/edit/delete interviews."""
from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import timedelta

from . import db
from .engine import locked, set_fields
from .matching import norm_company, norm_role
from .statuses import CATEGORY_STATUS, advance
from .timeutil import parse_dt, to_utc_iso

CARRY_FIELDS = ["location", "job_reference", "source", "contact_name", "contact_email", "salary_range", "job_url"]


class OpError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------------------------------
def recompute(conn: sqlite3.Connection, app_id: int) -> None:
    """Rebuild status and dates from an application's timeline and interviews (used after merge/split/edits).

    Hand-set (locked) fields are left alone. Status only ever comes from what the timeline supports.
    """
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        return
    status, applied, last_act, last_in = "Applied", None, None, None
    for ev in conn.execute("SELECT * FROM events WHERE application_id=? ORDER BY occurred_at, id", (app_id,)):
        status = advance(status, CATEGORY_STATUS.get(ev["category"]))
        if ev["email_id"] and applied is None:  # first *email*, not hand-added or calendar items
            applied = ev["occurred_at"]
        last_act = ev["occurred_at"] if not last_act or ev["occurred_at"] > last_act else last_act
        if ev["email_id"] and (not last_in or ev["occurred_at"] > last_in):
            last_in = ev["occurred_at"]
    if conn.execute("SELECT 1 FROM interviews WHERE application_id=? AND status<>'Cancelled'", (app_id,)).fetchone():
        status = advance(status, "Interviewing")
    vals = {"status": status, "applied_at": applied, "last_activity_at": last_act, "last_inbound_at": last_in}
    lk = locked(app)
    changes = {k: v for k, v in vals.items() if k not in lk and app[k] != v}
    if app["created_manually"] and "applied_at" in changes and applied is None:
        changes.pop("applied_at")  # keep the date you typed when there are no emails to derive one from
    if changes:
        cols = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE applications SET {cols}, updated_at=? WHERE id=?", (*changes.values(), db.utcnow(), app_id))


# ---------------------------------------------------------------------------------------------------
# Merge / split
# ---------------------------------------------------------------------------------------------------
def merge(conn: sqlite3.Connection, source_id: int, target_id: int, own_tx: bool = True) -> int:
    """Fold `source` into `target`: everything moves across, then the source application is deleted."""
    if source_id == target_id:
        raise OpError("Pick a different application to merge into.")
    src = conn.execute("SELECT * FROM applications WHERE id=?", (source_id,)).fetchone()
    tgt = conn.execute("SELECT * FROM applications WHERE id=?", (target_id,)).fetchone()
    if not src or not tgt:
        raise OpError("Application not found.")
    with (db.tx(conn) if own_tx else nullcontext()):
        for table in ("events", "emails", "interviews"):
            conn.execute(f"UPDATE {table} SET application_id=? WHERE application_id=?", (target_id, source_id))
        fill = {f: src[f] for f in CARRY_FIELDS + ["role_title"] if src[f]}
        set_fields(conn, "applications", tgt, fill, only_if_empty=True)
        if "role_title" in fill and not tgt["role_title"]:
            conn.execute("UPDATE applications SET role_norm=? WHERE id=?", (norm_role(src["role_title"]), target_id))
        if src["notes"]:
            note = f"{tgt['notes']}\n\n---\nMerged from {src['company']}:\n{src['notes']}".strip()
            conn.execute("UPDATE applications SET notes=? WHERE id=?", (note, target_id))
        conn.execute("DELETE FROM applications WHERE id=?", (source_id,))
        recompute(conn, target_id)
    return target_id


def split(conn: sqlite3.Connection, app_id: int, event_ids: list[int], interview_ids: list[int], company: str, role: str,
          own_tx: bool = True) -> int:
    """Move the chosen timeline events (and interviews) into a new application. Returns the new id."""
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        raise OpError("Application not found.")
    if not company.strip():
        raise OpError("The new application needs a company name.")
    own = {r["id"] for r in conn.execute("SELECT id FROM events WHERE application_id=?", (app_id,))}
    event_ids = [e for e in event_ids if e in own]
    own_iv = {r["id"] for r in conn.execute("SELECT id FROM interviews WHERE application_id=?", (app_id,))}
    interview_ids = [i for i in interview_ids if i in own_iv]
    if not event_ids and not interview_ids:
        raise OpError("Tick at least one item to move.")
    if len(event_ids) == len(own) and len(interview_ids) == len(own_iv):
        raise OpError("That would move everything. Edit the company and role instead.")
    now = db.utcnow()
    with (db.tx(conn) if own_tx else nullcontext()):
        cur = conn.execute(
            """INSERT INTO applications (company, company_norm, role_title, role_norm, status, locked_fields, created_at, updated_at)
               VALUES (?,?,?,?, 'Applied', ?, ?, ?)""",
            (company.strip(), norm_company(company), role.strip(), norm_role(role), json.dumps(["company", "role_title"]), now, now),
        )
        new_id = cur.lastrowid
        marks = ",".join("?" * len(event_ids))
        email_ids = [r["email_id"] for r in conn.execute(
            f"SELECT email_id FROM events WHERE id IN ({marks}) AND email_id IS NOT NULL", event_ids)] if event_ids else []
        if event_ids:
            conn.execute(f"UPDATE events SET application_id=? WHERE id IN ({marks})", (new_id, *event_ids))
        if email_ids:
            m2 = ",".join("?" * len(email_ids))
            conn.execute(f"UPDATE emails SET application_id=? WHERE id IN ({m2})", (new_id, *email_ids))
            conn.execute(f"UPDATE interviews SET application_id=? WHERE email_id IN ({m2}) AND application_id=?", (new_id, *email_ids, app_id))
        if interview_ids:
            m3 = ",".join("?" * len(interview_ids))
            conn.execute(f"UPDATE interviews SET application_id=? WHERE id IN ({m3})", (new_id, *interview_ids))
        recompute(conn, app_id)
        recompute(conn, new_id)
    return new_id


# ---------------------------------------------------------------------------------------------------
# Interviews by hand
# ---------------------------------------------------------------------------------------------------
KINDS = ("interview", "assessment")
STATES = ("New", "Scheduled", "Completed", "Cancelled")
FORMATS = ("", "phone", "video", "onsite", "unknown")


def _local_to_utc(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return to_utc_iso(value if len(value) > 16 else value + ":00")  # datetime-local values are UK local time
    except ValueError as e:
        raise OpError(f"Couldn't read the date/time '{value}'.") from e


def _clean(kind: str, status: str, start: str, end: str, stage: str, fmt: str) -> dict:
    if kind not in KINDS or status not in STATES or fmt not in FORMATS:
        raise OpError("Invalid interview details.")
    start_u, end_u = _local_to_utc(start), _local_to_utc(end)
    if start_u and not end_u:
        end_u = to_utc_iso(parse_dt(start_u) + timedelta(hours=1))
    if start_u and end_u and parse_dt(end_u) <= parse_dt(start_u):
        raise OpError("The end time must be after the start time.")
    if status == "Scheduled" and not start_u:
        raise OpError("A scheduled interview needs a date and time. Use 'New' when there isn't one yet.")
    if status == "New" and start_u:
        status = "Scheduled"
    return {"kind": kind, "status": status, "start_at": start_u, "end_at": end_u if start_u else None,
            "stage": stage.strip() or None, "format": fmt or None}


def create_interview(conn: sqlite3.Connection, app_id: int, kind: str, status: str, start: str, end: str, stage: str, fmt: str) -> int:
    if not conn.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone():
        raise OpError("Choose an application.")
    v = _clean(kind, status, start, end, stage, fmt)
    now = db.utcnow()
    with db.tx(conn):
        cur = conn.execute(
            """INSERT INTO interviews (application_id, kind, status, start_at, end_at, format, stage, locked_fields,
                                       created_manually, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
            (app_id, v["kind"], v["status"], v["start_at"], v["end_at"], v["format"], v["stage"],
             json.dumps(sorted(k for k in ("start_at", "end_at", "status", "stage", "format", "kind") if v[k])), now, now),
        )
        conn.execute("INSERT INTO events (application_id, occurred_at, category, summary) VALUES (?,?,?,?)",
                     (app_id, v["start_at"] or now, "interview_scheduled" if v["start_at"] else "interview_invite",
                      f"Added by hand: {v['kind']}"))
        recompute(conn, app_id)
    return cur.lastrowid


def update_interview(conn: sqlite3.Connection, interview_id: int, kind: str, status: str, start: str, end: str, stage: str, fmt: str) -> int:
    row = conn.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
    if not row:
        raise OpError("Interview not found.")
    v = _clean(kind, status, start, end, stage, fmt)
    changed = {k: val for k, val in v.items() if (row[k] or None) != (val or None)}
    if changed:
        lk = sorted(set(db.jloads(row["locked_fields"], [])) | set(changed))
        cols = ", ".join(f"{k}=?" for k in changed)
        with db.tx(conn):
            conn.execute(f"UPDATE interviews SET {cols}, locked_fields=?, auto_completed=0, updated_at=? WHERE id=?",
                         (*changed.values(), json.dumps(lk), db.utcnow(), interview_id))
            recompute(conn, row["application_id"])
    return row["application_id"]


def delete_interview(conn: sqlite3.Connection, interview_id: int) -> int | None:
    row = conn.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
    if not row:
        return None
    with db.tx(conn):
        conn.execute("DELETE FROM interviews WHERE id=?", (interview_id,))
        conn.execute("UPDATE calendar_events SET interview_id=NULL, state='dismissed' WHERE interview_id=?", (interview_id,))
    return row["application_id"]
