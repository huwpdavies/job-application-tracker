"""Calendar matching: link Outlook calendar events to interviews (read-only), and interview auto-completion.

The calendar is the source of truth for interview times: when an event matches an interview, the event's start/end
replace whatever the email said (unless that field was edited by hand).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .engine import Summary, set_fields
from .matching import norm_company
from .prefilter import DEFAULT_SENDER_DOMAINS, domain_matches, looks_like_interview_event
from .statuses import advance
from .timeutil import parse_dt, to_london, to_utc_iso

WINDOW_BEFORE = timedelta(days=round(6 * 30.44))
WINDOW_AFTER = timedelta(days=92)
TIME_TOLERANCE = timedelta(hours=36)       # event vs. the time the email gave
NEW_INTERVIEW_WINDOW = timedelta(days=45)  # event vs. an invite with no time agreed yet

FREE_OR_GENERIC = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "msn.com",
    "calendly.com", "zoom.us", "zoom.com",
}
_PUNCT = re.compile(r"[^a-z0-9]+")
_TWO_PART_TLDS = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz"}


def _squash(s: str) -> str:
    return _PUNCT.sub("", s.lower())


def _words(s: str) -> str:
    return " " + _PUNCT.sub(" ", s.lower()).strip() + " "


def registrable_label(domain: str) -> str:
    parts = domain.lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_TLDS:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


# ---------------------------------------------------------------------------------------------------
# Fetching and normalising
# ---------------------------------------------------------------------------------------------------
def fetch(g, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return g.calendar_view((now - WINDOW_BEFORE).strftime(fmt), (now + WINDOW_AFTER).strftime(fmt))


def normalise(ev: dict, me_addr: str) -> dict:
    org = ((ev.get("organizer") or {}).get("emailAddress") or {})
    atts = [((a.get("emailAddress") or {}).get("address") or "").lower() for a in ev.get("attendees") or []]
    addrs = {x for x in [(org.get("address") or "").lower(), *atts] if x and x != me_addr.lower()}
    return {
        "graph_id": ev["id"],
        "subject": ev.get("subject") or "(no title)",
        "start_at": to_utc_iso(ev["start"]["dateTime"], timezone.utc),
        "end_at": to_utc_iso(ev["end"]["dateTime"], timezone.utc),
        "organizer": org.get("name") or org.get("address") or "",
        "domains": sorted({a.rsplit("@", 1)[-1] for a in addrs}),
        "other_people": len(addrs),
        "web_link": ev.get("webLink") or "",
        "cancelled": bool(ev.get("isCancelled")),
        "all_day": bool(ev.get("isAllDay")),
        "text": " ".join(filter(None, [ev.get("subject"), (ev.get("location") or {}).get("displayName"),
                                       ev.get("bodyPreview"), org.get("name")])),
    }


# ---------------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------------
def _load_apps(conn: sqlite3.Connection) -> list[dict]:
    known: dict[int, set[str]] = {}
    for r in conn.execute("SELECT application_id, sender_domain FROM emails WHERE application_id IS NOT NULL AND sender_domain<>''"):
        d = r["sender_domain"]
        if d in FREE_OR_GENERIC or domain_matches(d, DEFAULT_SENDER_DOMAINS):
            continue
        known.setdefault(r["application_id"], set()).add(d)
    apps = []
    for r in conn.execute("SELECT id, company, company_norm, status FROM applications"):
        apps.append({
            "id": r["id"], "company": r["company"], "norm": r["company_norm"], "squash": _squash(r["company_norm"]),
            "status": r["status"], "domains": known.get(r["id"], set()),
        })
    return apps


def candidate_apps(apps: list[dict], ev: dict) -> list[dict]:
    """Applications whose company name appears in the event text, or whose domain appears among the attendees."""
    words = _words(ev["text"])
    labels = {registrable_label(d) for d in ev["domains"] if d not in FREE_OR_GENERIC}
    hits = []
    for a in apps:
        by_name = len(a["squash"]) >= 4 and (" " + _PUNCT.sub(" ", a["norm"]).strip() + " ") in words
        by_domain = bool(a["domains"] & set(ev["domains"])) or any(
            len(a["squash"]) >= 5 and len(l) >= 5 and (l == a["squash"] or l in a["squash"] or a["squash"] in l) for l in labels
        )
        if by_name or by_domain:
            hits.append(a)
    return hits


def _best_interview(conn: sqlite3.Connection, app_ids: list[int], ev: dict) -> sqlite3.Row | None:
    start = parse_dt(ev["start_at"])
    best, best_delta = None, None
    for r in conn.execute(
        f"SELECT * FROM interviews WHERE application_id IN ({','.join('?' * len(app_ids))}) AND status<>'Cancelled' AND kind='interview'",
        app_ids,
    ):
        if r["calendar_event_id"] and r["calendar_event_id"] != ev["graph_id"]:
            continue  # already tied to a different event
        if r["start_at"]:
            delta = abs(parse_dt(r["start_at"]) - start)
            if delta > TIME_TOLERANCE and r["calendar_event_id"] != ev["graph_id"]:
                continue
        else:
            created = parse_dt(r["created_at"], timezone.utc)
            if not (created - timedelta(days=1) <= start <= created + NEW_INTERVIEW_WINDOW):
                continue
            delta = timedelta(days=365)  # prefer interviews that already have a time
        if best is None or delta < best_delta:
            best, best_delta = r, delta
    return best


def link_event(conn: sqlite3.Connection, iv: sqlite3.Row, ev: dict) -> bool:
    """Make the event the source of truth for this interview. Returns True if anything changed."""
    vals = {"start_at": ev["start_at"], "end_at": ev["end_at"], "calendar_event_id": ev["graph_id"]}
    if iv["status"] in ("New", "Scheduled"):
        vals["status"] = "Cancelled" if ev["cancelled"] else "Scheduled"
        vals["auto_completed"] = 0
    changed = set_fields(conn, "interviews", iv, vals)
    _drop_superseded(conn, iv, ev)
    return bool(changed)


def _drop_superseded(conn: sqlite3.Connection, iv: sqlite3.Row, ev: dict) -> None:
    """The calendar event is the truth. Other un-linked interviews for the same application on the same UK day
    came from earlier emails with a time that was later changed, so they are stale duplicates."""
    day = to_london(ev["start_at"]).date()
    for r in conn.execute(
        "SELECT * FROM interviews WHERE application_id=? AND id<>? AND calendar_event_id IS NULL "
        "AND created_manually=0 AND start_at IS NOT NULL", (iv["application_id"], iv["id"])).fetchall():
        if to_london(r["start_at"]).date() == day:
            conn.execute("DELETE FROM interviews WHERE id=?", (r["id"],))


def process(conn: sqlite3.Connection, raw_events: list[dict], me_addr: str, summary: Summary) -> None:
    apps = _load_apps(conn)
    for raw in raw_events:
        ev = normalise(raw, me_addr)
        if ev["all_day"]:
            continue
        row = conn.execute("SELECT * FROM calendar_events WHERE graph_id=?", (ev["graph_id"],)).fetchone()
        if row and row["state"] == "dismissed":
            continue
        cands = candidate_apps(apps, ev)
        iv = _best_interview(conn, [a["id"] for a in cands], ev) if cands else None
        if row and row["interview_id"] and not iv:
            iv = conn.execute("SELECT * FROM interviews WHERE id=?", (row["interview_id"],)).fetchone()

        state = "seen"
        if iv:
            state = "matched"
            if link_event(conn, iv, ev):
                summary.log(f"CALENDAR matched '{ev['subject'][:50]}' to interview #{iv['id']} ({ev['start_at'][:16]}Z)")
                summary.calendar_matched += 1
            _bump_status(conn, iv["application_id"])
        elif not ev["cancelled"] and (cands or ev["other_people"]) and looks_like_interview_event(ev["subject"], ev["text"]):
            state = "review"

        _upsert_row(conn, ev, state, iv["id"] if iv else None)
        if state == "review":
            _ensure_review(conn, ev, cands, summary)
        else:
            conn.execute("UPDATE review_items SET state='linked' WHERE kind='calendar' AND calendar_event_id=? AND state='open'",
                         (ev["graph_id"],))


def _bump_status(conn: sqlite3.Connection, app_id: int) -> None:
    a = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    new = advance(a["status"], "Interviewing")
    if new != a["status"]:
        set_fields(conn, "applications", a, {"status": new})


def _upsert_row(conn, ev: dict, state: str, interview_id: int | None) -> None:
    conn.execute(
        """INSERT INTO calendar_events (graph_id, subject, start_at, end_at, organizer, attendee_domains, web_link, interview_id, state)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(graph_id) DO UPDATE SET subject=excluded.subject, start_at=excluded.start_at, end_at=excluded.end_at,
               organizer=excluded.organizer, attendee_domains=excluded.attendee_domains, web_link=excluded.web_link,
               interview_id=excluded.interview_id, state=excluded.state""",
        (ev["graph_id"], ev["subject"], ev["start_at"], ev["end_at"], ev["organizer"], json.dumps(ev["domains"]),
         ev["web_link"], interview_id, state),
    )


def _ensure_review(conn, ev: dict, cands: list[dict], summary: Summary) -> None:
    if conn.execute("SELECT 1 FROM review_items WHERE kind='calendar' AND calendar_event_id=? AND state='open'",
                    (ev["graph_id"],)).fetchone():
        return
    sug = {"candidate_application_ids": [a["id"] for a in cands]}
    reason = "Looks like an interview, but no matching email or interview" + (" (company recognised)" if cands else "")
    conn.execute(
        "INSERT INTO review_items (kind, calendar_event_id, reason, suggestion, created_at) VALUES ('calendar',?,?,?,?)",
        (ev["graph_id"], reason, json.dumps(sug), db.utcnow()),
    )
    summary.needs_review += 1
    summary.calendar_review += 1
    summary.log(f"REVIEW   calendar event '{ev['subject'][:50]}' ({ev['start_at'][:10]}): {reason}")


# ---------------------------------------------------------------------------------------------------
# Auto-complete (with undo)
# ---------------------------------------------------------------------------------------------------
def auto_complete(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Scheduled interviews whose end time has passed become Completed. Hand-set statuses are left alone."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for r in conn.execute("SELECT * FROM interviews WHERE status='Scheduled' AND start_at IS NOT NULL").fetchall():
        end = parse_dt(r["end_at"]) if r["end_at"] else parse_dt(r["start_at"]) + timedelta(hours=1)
        if end <= now and "status" not in db.jloads(r["locked_fields"], []):
            conn.execute("UPDATE interviews SET status='Completed', auto_completed=1, updated_at=? WHERE id=?", (db.utcnow(), r["id"]))
            n += 1
    return n


def undo_complete(conn: sqlite3.Connection, interview_id: int) -> bool:
    """Put an auto-completed interview back to Scheduled and lock the status so it isn't completed again."""
    r = conn.execute("SELECT * FROM interviews WHERE id=? AND auto_completed=1 AND status='Completed'", (interview_id,)).fetchone()
    if not r:
        return False
    lk = sorted(set(db.jloads(r["locked_fields"], [])) | {"status"})
    conn.execute("UPDATE interviews SET status='Scheduled', auto_completed=0, locked_fields=?, updated_at=? WHERE id=?",
                 (json.dumps(lk), db.utcnow(), interview_id))
    return True


# ---------------------------------------------------------------------------------------------------
# Review actions for calendar items
# ---------------------------------------------------------------------------------------------------
class CalendarReviewError(ValueError):
    pass


def accept_event(conn: sqlite3.Connection, item_id: int, app_id: int | None, company: str = "", role: str = "") -> int:
    """Turn a calendar review item into an interview on an existing application (or a new one)."""
    item = conn.execute("SELECT * FROM review_items WHERE id=? AND kind='calendar' AND state='open'", (item_id,)).fetchone()
    if not item:
        raise CalendarReviewError("That review item has already been handled.")
    ev = conn.execute("SELECT * FROM calendar_events WHERE graph_id=?", (item["calendar_event_id"],)).fetchone()
    now = db.utcnow()
    with db.tx(conn):
        if app_id is None:
            if not company.strip():
                raise CalendarReviewError("Choose an existing application, or enter a company for a new one.")
            cur = conn.execute(
                """INSERT INTO applications (company, company_norm, role_title, role_norm, status, last_activity_at,
                       created_at, updated_at) VALUES (?,?,?,?,'Applied',?,?,?)""",
                (company.strip(), norm_company(company), role.strip(), re.sub(r"\s+", " ", role.strip().lower()), ev["start_at"], now, now),
            )
            app_id = cur.lastrowid
        elif not conn.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone():
            raise CalendarReviewError("That application no longer exists.")
        cur = conn.execute(
            """INSERT INTO interviews (application_id, status, start_at, end_at, calendar_event_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (app_id, "Scheduled", ev["start_at"], ev["end_at"], ev["graph_id"], now, now),
        )
        conn.execute(
            "INSERT INTO events (application_id, occurred_at, category, summary, web_link) VALUES (?,?,?,?,?)",
            (app_id, ev["start_at"], "interview_scheduled", f"Calendar: {ev['subject']}", ev["web_link"]),
        )
        _bump_status(conn, app_id)
        conn.execute("UPDATE calendar_events SET interview_id=?, state='matched' WHERE graph_id=?", (cur.lastrowid, ev["graph_id"]))
        conn.execute("UPDATE review_items SET state='accepted' WHERE id=?", (item_id,))
    return app_id


def dismiss_event(conn: sqlite3.Connection, item_id: int) -> None:
    item = conn.execute("SELECT * FROM review_items WHERE id=? AND kind='calendar' AND state='open'", (item_id,)).fetchone()
    if not item:
        raise CalendarReviewError("That review item has already been handled.")
    with db.tx(conn):
        conn.execute("UPDATE calendar_events SET state='dismissed' WHERE graph_id=?", (item["calendar_event_id"],))
        conn.execute("UPDATE review_items SET state='dismissed' WHERE id=?", (item_id,))
