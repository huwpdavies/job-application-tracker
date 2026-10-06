"""Shared test helpers: fake Graph, fake Claude, and builders for messages/classifications/events."""
from __future__ import annotations

import itertools

from tracker import db
from tracker.classify import Classification, Result, Usage
from tracker.engine import Summary, handle_email
from tracker.mail import Message, message_from_graph
from tracker.timeutil import to_utc_iso

_ids = itertools.count(1)


def classification(fix: dict, **override) -> Classification:
    return Classification.model_validate({**fix["classification"], **override})


def graph_raw(fix: dict, gid: str | None = None, internet_id: str | None = None, folder: str = "F1") -> dict:
    n = next(_ids)
    return {
        "id": gid or f"AAA{n}", "internetMessageId": internet_id or f"<msg{n}@test>", "subject": fix["subject"],
        "from": {"emailAddress": {"address": fix["sender"]}}, "receivedDateTime": fix["received"],
        "body": {"contentType": "html", "content": fix["html"]}, "webLink": f"https://outlook.live.com/owa/?ItemID={n}",
        "parentFolderId": folder,
    }


def message(fix: dict, **kw) -> Message:
    return message_from_graph(graph_raw(fix, **kw))


def run_emails(conn, fixes: list[dict], threshold: float = 0.7, folder: str = "applications", **cls_override) -> Summary:
    """Feed fixtures through the real engine (oldest first, as the sync does), with Claude's answer taken from the fixture."""
    summary = Summary()
    for fix in sorted(fixes, key=lambda f: f["received"]):
        with db.tx(conn):
            handle_email(conn, message(fix), folder, classification(fix, **cls_override), threshold, summary)
    return summary


def raw_event(eid: str, subject: str, start_utc: str, end_utc: str, attendees=(), organizer="Recruiter",
              organizer_addr="", cancelled=False, preview="", all_day=False) -> dict:
    return {
        "id": eid, "subject": subject, "isCancelled": cancelled, "isAllDay": all_day, "bodyPreview": preview,
        "start": {"dateTime": start_utc.replace("Z", "") + ".0000000", "timeZone": "UTC"},
        "end": {"dateTime": end_utc.replace("Z", "") + ".0000000", "timeZone": "UTC"},
        "organizer": {"emailAddress": {"name": organizer, "address": organizer_addr}},
        "attendees": [{"emailAddress": {"address": a}} for a in attendees], "webLink": f"https://outlook.live.com/calendar/{eid}",
        "location": {"displayName": ""},
    }


def app_row(conn, company: str, role: str = "", status: str = "Applied", ref: str | None = None, **extra) -> int:
    from tracker.matching import norm_company, norm_role

    now = db.utcnow()
    cur = conn.execute(
        """INSERT INTO applications (company, company_norm, role_title, role_norm, job_reference, status, applied_at,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (company, norm_company(company), role, norm_role(role), ref, status, extra.get("applied_at", "2026-09-01T09:00:00+00:00"), now, now),
    )
    return cur.lastrowid


class FakeClassifier:
    """Stands in for tracker.classify.Classifier: answers from a {subject: Classification | Exception} map."""

    calls: list[str] = []
    answers: dict = {}

    def __init__(self, api_key, model):
        pass

    def classify(self, sender, subject, received_utc, text):
        FakeClassifier.calls.append(subject)
        ans = FakeClassifier.answers.get(subject)
        if isinstance(ans, str):
            return Result(None, Usage(10, 5), ans)
        if ans is None:
            return Result(None, Usage(0, 0), "no canned answer")
        return Result(ans, Usage(1000, 150))


class FakeGraph:
    """Just enough of GraphClient for sync.run: folders, delta, inbox, single-message fetch, calendar."""

    def __init__(self):
        self.delta_batches: list[list[dict]] = []   # one list returned per delta call
        self.delta_calls: list[tuple] = []
        self.inbox: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.events: list[dict] = []
        self.me_addr = "huw@example.com"
        self.folder_name = "Job Applications"
        self.calendar_error = None

    def me(self):
        return {"mail": self.me_addr, "displayName": "Huw"}

    def find_folder(self, name):
        return {"id": "F1", "displayName": name} if name.lower() == self.folder_name.lower() else None

    def paged(self, path, params=None, headers=None):
        if path.endswith("/childFolders"):
            return iter([])
        if "/inbox/messages" in path:
            return iter(self.inbox)
        return iter([])

    def get(self, path, params=None, headers=None):
        mid = path.rsplit("/", 1)[-1]
        if mid in self.by_id:
            return self.by_id[mid]
        from tracker.graph import GraphError

        raise GraphError(f"404 {path}")

    def delta_messages(self, folder_id, select, saved_link, since_iso):
        self.delta_calls.append((folder_id, saved_link, since_iso))
        batch = self.delta_batches.pop(0) if self.delta_batches else []
        for m in batch:
            self.by_id[m["id"]] = m
        return batch, f"DELTA-{len(self.delta_calls)}"

    def calendar_view(self, start, end):
        if self.calendar_error:
            raise self.calendar_error
        return self.events


def utc(s: str) -> str:
    return to_utc_iso(s)
