"""Turn a scheduled interview into a Google Calendar event.

There is deliberately no Google sign-in and no API call: the app builds a pre-filled "new event" link that opens in
your own browser (you press Save in Google Calendar), plus a standard .ics file as a fallback for any calendar app.
Only the title, time, stage/format and job-ad link are included. No email text ever goes into an event.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .config import LONDON
from .timeutil import parse_dt

GOOGLE_NEW_EVENT = "https://calendar.google.com/calendar/render"
DEFAULT_LENGTH = timedelta(hours=1)
LOCATIONS = {"video": "Video call", "phone": "Phone call"}


class NotEligible(ValueError):
    """The interview can't become a calendar event (no time yet, cancelled, an assessment...)."""


def _stamp(iso: str) -> str:
    return parse_dt(iso).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_fields(iv: sqlite3.Row, app: sqlite3.Row) -> dict:
    if iv["kind"] != "interview":
        raise NotEligible("Assessments have no set time, so there is nothing to put in a calendar.")
    if not iv["start_at"]:
        raise NotEligible("This interview has no date and time yet.")
    if iv["status"] != "Scheduled":
        raise NotEligible(f"Only scheduled interviews can be added (this one is {iv['status'].lower()}).")
    start = parse_dt(iv["start_at"])
    end = parse_dt(iv["end_at"]) if iv["end_at"] else start + DEFAULT_LENGTH
    role = (app["role_title"] or "").strip()
    title = f"Interview: {role} at {app['company']}" if role else f"Interview at {app['company']}"
    notes = []
    if iv["stage"]:
        notes.append(f"Stage: {iv['stage']}")
    if iv["format"] and iv["format"] != "unknown":
        notes.append(f"Format: {iv['format']}")
    if role:
        notes.append(f"Role: {role}")
    if app["job_url"]:
        notes.append(f"Job ad: {app['job_url']}")
    notes.append("Added from Job Tracker")
    return {
        "title": title, "details": "\n".join(notes), "location": LOCATIONS.get(iv["format"] or "", ""),
        "start": start.astimezone(timezone.utc), "end": end.astimezone(timezone.utc),
        "uid": f"interview-{iv['id']}@job-tracker.local",
    }


def google_url(iv: sqlite3.Row, app: sqlite3.Row) -> str:
    e = event_fields(iv, app)
    params = {
        "action": "TEMPLATE", "text": e["title"], "details": e["details"], "ctz": LONDON,
        "dates": f"{e['start'].strftime('%Y%m%dT%H%M%SZ')}/{e['end'].strftime('%Y%m%dT%H%M%SZ')}",
    }
    if e["location"]:
        params["location"] = e["location"]
    return GOOGLE_NEW_EVENT + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines are at most 75 octets; continuation lines start with a space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def ics(iv: sqlite3.Row, app: sqlite3.Row, now: datetime | None = None) -> str:
    e = event_fields(iv, app)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Job Tracker//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{stamp}",
        f"DTSTART:{e['start'].strftime('%Y%m%dT%H%M%SZ')}", f"DTEND:{e['end'].strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_esc(e['title'])}", f"DESCRIPTION:{_esc(e['details'])}",
    ]
    if e["location"]:
        lines.append(f"LOCATION:{_esc(e['location'])}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"
