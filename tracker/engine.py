"""Turns classified emails into applications, timeline events and interviews.

`handle_email` decides what to do with one classified message; `commit_to_application` does the writing and is
also what the Needs-review "accept / link" actions call later.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from . import db
from .classify import Classification
from .mail import Message
from .matching import find_match, norm_company, norm_role
from .statuses import CATEGORY_STATUS, INTERVIEW_CATEGORIES, advance
from .timeutil import parse_dt, to_london, to_utc_iso

DEFAULT_INTERVIEW_LENGTH = timedelta(hours=1)
CONFIRMS_APPLIED = {"application_confirmation", "acknowledgement"}
CREATES_APPLICATION = CONFIRMS_APPLIED | {
    "interview_invite", "interview_scheduled", "interview_rescheduled", "assessment", "offer", "rejection",
}


@dataclass
class Summary:
    new_applications: int = 0
    updated_applications: int = 0
    interviews_found: int = 0
    needs_review: int = 0
    skipped_known: int = 0
    other: int = 0
    errors: int = 0
    calendar_matched: int = 0
    calendar_review: int = 0
    auto_completed: int = 0
    actions: list[str] = field(default_factory=list)

    def log(self, text: str) -> None:
        self.actions.append(text)


# ---------------------------------------------------------------------------------------------------
# Locked-field aware updates
# ---------------------------------------------------------------------------------------------------
def locked(row: sqlite3.Row) -> set[str]:
    return set(db.jloads(row["locked_fields"], []))


def set_fields(conn: sqlite3.Connection, table: str, row: sqlite3.Row, values: dict, only_if_empty: bool = False) -> dict:
    """Apply automatic updates, skipping hand-edited (locked) fields. Returns what actually changed."""
    lk = locked(row)
    changes = {}
    for k, v in values.items():
        if k in lk or v is None:
            continue
        cur = row[k]
        if only_if_empty and cur not in (None, ""):
            continue
        if cur != v:
            changes[k] = v
    if changes:
        cols = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE {table} SET {cols}, updated_at=? WHERE id=?", (*changes.values(), db.utcnow(), row["id"]))
    return changes


# ---------------------------------------------------------------------------------------------------
# Writing to an application
# ---------------------------------------------------------------------------------------------------
def create_application(conn: sqlite3.Connection, cls: Classification, received_at: str) -> int:
    applied_at = received_at  # the date of the first email we have for it
    now = db.utcnow()
    cur = conn.execute(
        """INSERT INTO applications (company, company_norm, role_title, role_norm, location, job_reference, source,
                                     status, applied_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cls.company, norm_company(cls.company), cls.role_title or "", norm_role(cls.role_title), cls.location,
            cls.job_reference, cls.source, "Applied", applied_at, now, now,
        ),
    )
    return cur.lastrowid


def commit_to_application(
    conn: sqlite3.Connection,
    email_id: int,
    cls: Classification,
    received_at: str,
    web_link: str,
    app_id: int | None,
    inbound: bool = True,
    summary: Summary | None = None,
) -> int:
    """Attach an email to an application (creating it if app_id is None) and apply all consequences."""
    summary = summary or Summary()
    created = app_id is None
    if created:
        app_id = create_application(conn, cls, received_at)
        summary.new_applications += 1
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()

    conn.execute("UPDATE emails SET application_id=?, state='processed' WHERE id=?", (app_id, email_id))
    conn.execute(
        "INSERT INTO events (application_id, email_id, occurred_at, category, summary, web_link) VALUES (?,?,?,?,?,?)",
        (app_id, email_id, received_at, cls.category, cls.summary, web_link),
    )

    changes: dict = {}
    # Fill gaps without overwriting what's there (and never touching locked fields).
    changes.update(set_fields(conn, "applications", app, {
        "location": cls.location, "job_reference": cls.job_reference, "source": cls.source,
    }, only_if_empty=True))
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app["role_title"] and cls.role_title:
        changes.update(set_fields(conn, "applications", app, {"role_title": cls.role_title, "role_norm": norm_role(cls.role_title)}))

    # Interviews
    interview_status = None
    if cls.category in INTERVIEW_CATEGORIES:
        interview_status = upsert_interview(conn, app_id, email_id, cls, summary)

    # Status: forward only
    target = CATEGORY_STATUS.get(cls.category)
    if cls.category == "interview_cancelled":
        target = None
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    new_status = advance(app["status"], target)
    if new_status != app["status"] and "status" not in locked(app):
        changes["status"] = new_status

    # Timestamps
    if not app["applied_at"] or received_at < app["applied_at"]:
        changes["applied_at"] = received_at  # "Applied on" is simply the date of the first email
    if not app["last_activity_at"] or received_at > app["last_activity_at"]:
        changes["last_activity_at"] = received_at
    if inbound and (not app["last_inbound_at"] or received_at > app["last_inbound_at"]):
        changes["last_inbound_at"] = received_at
    if changes:
        cols = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE applications SET {cols}, updated_at=? WHERE id=?", (*changes.values(), db.utcnow(), app_id))
    if not created:
        summary.updated_applications += 1
    return app_id


def upsert_assessment(conn: sqlite3.Connection, app_id: int, email_id: int, cls: Classification, summary: Summary) -> str:
    """Tests, video questions and technical assessments count as interviews but need no date or time."""
    row = conn.execute(
        "SELECT * FROM interviews WHERE application_id=? AND kind='assessment' AND status='New' ORDER BY id DESC",
        (app_id,),
    ).fetchone()
    if row:  # a reminder or follow-up about the same assessment
        set_fields(conn, "interviews", row, {"format": cls.interview_format, "stage": cls.interview_stage}, only_if_empty=True)
        return "New"
    now = db.utcnow()
    conn.execute(
        """INSERT INTO interviews (application_id, email_id, kind, status, format, stage, created_at, updated_at)
           VALUES (?,?, 'assessment', 'New', ?, ?, ?, ?)""",
        (app_id, email_id, cls.interview_format, cls.interview_stage, now, now),
    )
    summary.interviews_found += 1
    return "New"


def upsert_interview(conn: sqlite3.Connection, app_id: int, email_id: int, cls: Classification, summary: Summary) -> str | None:
    now = db.utcnow()
    if cls.category == "assessment":
        return upsert_assessment(conn, app_id, email_id, cls, summary)
    open_rows = conn.execute(
        "SELECT * FROM interviews WHERE application_id=? AND kind='interview' AND status IN ('New','Scheduled') "
        "ORDER BY created_at DESC, id DESC",
        (app_id,),
    ).fetchall()
    start = cls.interview_datetime
    stage = (cls.interview_stage or "").strip().lower()

    if cls.category == "interview_cancelled":
        row = next((r for r in open_rows if start and r["start_at"] == start), open_rows[0] if open_rows else None)
        if row:
            set_fields(conn, "interviews", row, {"status": "Cancelled"})
        return "Cancelled" if row else None

    row = None
    for r in open_rows:
        same_stage = bool(stage) and stage == (r["stage"] or "").lower()
        # A new time on the same (UK) day, with no conflicting stage, is a change to the same interview.
        same_day = bool(start and r["start_at"]) and to_london(start).date() == to_london(r["start_at"]).date() and (
            not stage or not r["stage"] or same_stage
        )
        if r["status"] == "New" or cls.category == "interview_rescheduled" or same_stage or same_day or (start and r["start_at"] == start):
            row = r
            break
    end = to_utc_iso(parse_dt(start) + DEFAULT_INTERVIEW_LENGTH) if start else None
    if row is None:
        conn.execute(
            """INSERT INTO interviews (application_id, email_id, status, start_at, end_at, format, stage, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (app_id, email_id, "Scheduled" if start else "New", start, end, cls.interview_format, cls.interview_stage, now, now),
        )
        summary.interviews_found += 1
        return "Scheduled" if start else "New"
    vals = {"format": cls.interview_format, "stage": cls.interview_stage, "email_id": email_id}
    if start:
        vals.update({"start_at": start, "end_at": end, "status": "Scheduled"})
    # format/stage: only fill if empty unless this is a reschedule/refresh
    set_fields(conn, "interviews", row, {k: v for k, v in vals.items() if k in ("start_at", "end_at", "status", "email_id")})
    set_fields(conn, "interviews", row, {k: v for k, v in vals.items() if k in ("format", "stage")}, only_if_empty=True)
    return "Scheduled" if start else row["status"]


# ---------------------------------------------------------------------------------------------------
# Deciding what to do with a classified email
# ---------------------------------------------------------------------------------------------------
def find_existing(conn: sqlite3.Connection, internet_id: str | None, graph_id: str) -> sqlite3.Row | None:
    row = None
    if internet_id:
        row = conn.execute("SELECT * FROM emails WHERE internet_message_id=?", (internet_id,)).fetchone()
    return row or conn.execute("SELECT * FROM emails WHERE graph_id=?", (graph_id,)).fetchone()


def note_moved(conn: sqlite3.Connection, existing: sqlite3.Row, m: Message, folder: str) -> bool:
    """A message we've already processed shows up again (moved from Inbox into the folder, new Graph id)."""
    changed = existing["graph_id"] != m.graph_id or (existing["folder"] != folder and folder == "applications")
    if changed:
        clash = conn.execute("SELECT id FROM emails WHERE graph_id=? AND id<>?", (m.graph_id, existing["id"])).fetchone()
        if not clash:
            conn.execute(
                "UPDATE emails SET graph_id=?, web_link=?, folder=?, not_filed_yet=? WHERE id=?",
                (
                    m.graph_id, m.web_link or existing["web_link"],
                    "applications" if folder == "applications" else existing["folder"],
                    0 if folder == "applications" else existing["not_filed_yet"], existing["id"],
                ),
            )
    return changed


def insert_email(conn: sqlite3.Connection, m: Message, folder: str, cls: Classification | None, state: str) -> int:
    cur = conn.execute(
        """INSERT INTO emails (graph_id, internet_message_id, folder, sender, sender_domain, subject, received_at, web_link,
                               category, is_job_related, confidence, classification, not_filed_yet, state, processed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            m.graph_id, m.internet_message_id, folder, m.sender, m.sender_domain, m.subject, m.received_at, m.web_link,
            cls.category if cls else None, int(cls.is_job_related) if cls else None, cls.confidence if cls else None,
            cls.model_dump_json() if cls else None, int(folder == "inbox"), state, db.utcnow(),
        ),
    )
    return cur.lastrowid


def add_review(conn: sqlite3.Connection, email_id: int, reason: str, suggestion: dict) -> None:
    conn.execute(
        "INSERT INTO review_items (kind, email_id, reason, suggestion, created_at) VALUES ('email',?,?,?,?)",
        (email_id, reason, json.dumps(suggestion), db.utcnow()),
    )


def handle_email(
    conn: sqlite3.Connection,
    m: Message,
    folder: str,
    cls: Classification | None,
    threshold: float,
    summary: Summary,
    me_address: str = "",
    error: str | None = None,
) -> None:
    label = f"{m.subject[:55]}"
    if cls is None:
        insert_email(conn, m, folder, None, "error")
        summary.errors += 1
        summary.log(f"ERROR    {label}  ({error})")
        return

    inbound = bool(m.sender) and m.sender.lower() != me_address.lower()
    suggestion = cls.model_dump()

    if cls.confidence < threshold:
        eid = insert_email(conn, m, folder, cls, "review")
        add_review(conn, eid, f"Low confidence ({cls.confidence:.2f})", suggestion)
        summary.needs_review += 1
        summary.log(f"REVIEW   low confidence {cls.confidence:.2f}: {label}")
        return

    if cls.category == "other" or not cls.is_job_related:
        insert_email(conn, m, folder, cls, "processed")
        summary.other += 1
        summary.log(f"IGNORE   not an application email: {label}")
        return

    if not cls.company:
        eid = insert_email(conn, m, folder, cls, "review")
        add_review(conn, eid, "Company not identified", suggestion)
        summary.needs_review += 1
        summary.log(f"REVIEW   no company: {label}")
        return

    match = find_match(conn, cls.company, cls.role_title, cls.job_reference)
    # These categories are proof an application exists (you can't be invited to interview for a job you didn't
    # apply for, or rejected from one), so they may create it. Recruiter messages and cancellations must attach to
    # an existing application, else Needs review.
    starts_application = cls.category in CREATES_APPLICATION
    app_id = match.app_id

    if app_id is None:
        can_create = starts_application and not match.ambiguous
        if not can_create:
            reason = (
                "Several applications at this company; can't tell which" if match.ambiguous
                else ("No matching application (company known, role differs)" if match.candidates else "No matching application")
            )
            eid = insert_email(conn, m, folder, cls, "review")
            add_review(conn, eid, reason, {**suggestion, "candidate_application_ids": match.candidates})
            summary.needs_review += 1
            summary.log(f"REVIEW   {reason}: {cls.company} / {cls.role_title} [{cls.category}]")
            return

    eid = insert_email(conn, m, folder, cls, "processed")
    before_new = summary.new_applications
    app_id = commit_to_application(conn, eid, cls, m.received_at, m.web_link, app_id, inbound, summary)
    tag = "CREATE  " if summary.new_applications > before_new else "UPDATE  "
    row = conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()
    summary.log(f"{tag} {cls.company} / {cls.role_title or '-'}  <- {cls.category}  (status {row['status']})")
