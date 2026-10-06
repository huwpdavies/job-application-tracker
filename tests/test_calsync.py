"""Calendar matching, calendar-as-truth, interview auto-completion and its undo."""
import json
from datetime import datetime, timezone

import fixtures as fx
from helpers import raw_event, run_emails
from tracker import calsync, db
from tracker.engine import Summary


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def seeded(conn):
    """Brightwater Energy interview, scheduled by email for 13 Oct 14:00 BST (13:00Z)."""
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME])
    return one(conn, "SELECT id FROM interviews")[0]


def process(conn, *events):
    s = Summary()
    with db.tx(conn):
        calsync.process(conn, list(events), "huw@example.com", s)
    return s


def test_event_matched_by_company_name_in_title(conn):
    iid = seeded(conn)
    s = process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:00:00Z", "2026-10-13T13:45:00Z"))
    iv = one(conn, "SELECT * FROM interviews WHERE id=?", iid)
    assert iv["calendar_event_id"] == "E1" and iv["start_at"] == "2026-10-13T13:00:00+00:00"
    assert one(conn, "SELECT state FROM calendar_events")[0] == "matched"
    assert one(conn, "SELECT COUNT(*) FROM review_items")[0] == 0


def test_calendar_time_overrides_the_email_time(conn):
    iid = seeded(conn)
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:15:00Z", "2026-10-13T14:00:00Z"))
    iv = one(conn, "SELECT * FROM interviews WHERE id=?", iid)
    assert iv["start_at"] == "2026-10-13T13:15:00+00:00" and iv["end_at"] == "2026-10-13T14:00:00+00:00"
    assert iv["status"] == "Scheduled"


def test_event_matched_by_attendee_domain_when_title_has_no_company(conn):
    iid = seeded(conn)   # the email came from @brightwaterenergy.com
    process(conn, raw_event("E1", "Intro chat", "2026-10-13T13:00:00Z", "2026-10-13T13:30:00Z",
                            attendees=["jessica.moore@brightwaterenergy.com", "huw@example.com"]))
    assert one(conn, "SELECT calendar_event_id FROM interviews WHERE id=?", iid)[0] == "E1"


def test_manually_set_time_is_not_overwritten_by_the_calendar(conn):
    iid = seeded(conn)
    conn.execute("UPDATE interviews SET locked_fields=? WHERE id=?", (json.dumps(["start_at", "end_at"]), iid))
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:20:00Z", "2026-10-13T14:00:00Z"))
    assert one(conn, "SELECT start_at FROM interviews WHERE id=?", iid)[0] == "2026-10-13T13:00:00+00:00"


def test_event_far_from_the_email_time_is_not_matched_and_goes_to_review(conn):
    seeded(conn)
    s = process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z"))
    assert one(conn, "SELECT interview_id FROM calendar_events")[0] is None
    assert s.calendar_review == 1 and "company recognised" in one(conn, "SELECT reason FROM review_items")[0]


def test_interview_like_event_with_unknown_company_goes_to_review(conn):
    s = process(conn, raw_event("E2", "Screening call Huw/Lucie", "2026-10-14T09:00:00Z", "2026-10-14T09:30:00Z", attendees=["lucie@unknown.io"]))
    assert s.calendar_review == 1 and one(conn, "SELECT kind FROM review_items")[0] == "calendar"


def test_personal_events_are_ignored(conn):
    s = process(conn, raw_event("E3", "Dentist", "2026-10-14T09:00:00Z", "2026-10-14T10:00:00Z"),
                raw_event("E4", "Interview prep (just me)", "2026-10-14T11:00:00Z", "2026-10-14T12:00:00Z"),   # keyword but nobody else invited
                raw_event("E5", "Team away day", "2026-10-15", "2026-10-16", all_day=True))
    assert s.calendar_review == 0 and one(conn, "SELECT COUNT(*) FROM review_items")[0] == 0


def test_cancelled_event_cancels_the_interview(conn):
    iid = seeded(conn)
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:00:00Z", "2026-10-13T13:45:00Z", cancelled=True))
    assert one(conn, "SELECT status FROM interviews WHERE id=?", iid)[0] == "Cancelled"


def test_review_item_resolves_itself_when_a_matching_interview_appears(conn):
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:00:00Z", "2026-10-13T13:45:00Z", attendees=["x@brightwaterenergy.com"]))
    assert one(conn, "SELECT state FROM review_items")[0] == "open"
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME])           # the email shows up on a later sync
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:00:00Z", "2026-10-13T13:45:00Z", attendees=["x@brightwaterenergy.com"]))
    assert one(conn, "SELECT state FROM review_items")[0] == "linked"


def test_stale_same_day_duplicates_are_dropped_once_the_calendar_confirms(conn):
    iid = seeded(conn)
    conn.execute("""INSERT INTO interviews (application_id, kind, status, start_at, end_at, created_at, updated_at)
                    SELECT application_id,'interview','Scheduled','2026-10-13T11:00:00+00:00','2026-10-13T12:00:00+00:00',?,? FROM interviews WHERE id=?""",
                 (db.utcnow(), db.utcnow(), iid))
    assert one(conn, "SELECT COUNT(*) FROM interviews")[0] == 2
    process(conn, raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:00:00Z", "2026-10-13T13:45:00Z"))
    rows = conn.execute("SELECT * FROM interviews").fetchall()
    assert len(rows) == 1 and rows[0]["calendar_event_id"] == "E1"


def test_assessments_are_never_matched_to_calendar_events(conn):
    run_emails(conn, [fx.ASSESSMENT])
    process(conn, raw_event("E1", "Calder Retail Group call", "2026-10-09T10:00:00Z", "2026-10-09T10:30:00Z"))
    assert one(conn, "SELECT calendar_event_id FROM interviews")[0] is None


def test_accept_and_dismiss_calendar_review_items(conn):
    run_emails(conn, [fx.REJECTION])
    aid = one(conn, "SELECT id FROM applications")[0]
    process(conn, raw_event("E1", "Interview with Harlow", "2026-10-14T09:00:00Z", "2026-10-14T09:30:00Z", attendees=["a@harlowandpine.co.uk"]),
            raw_event("E2", "Screening call", "2026-10-15T09:00:00Z", "2026-10-15T09:30:00Z", attendees=["z@else.com"]))
    items = conn.execute("SELECT id, calendar_event_id FROM review_items ORDER BY id").fetchall()
    calsync.accept_event(conn, items[0]["id"], aid)
    iv = one(conn, "SELECT * FROM interviews WHERE application_id=?", aid)
    assert iv["start_at"] == "2026-10-14T09:00:00+00:00" and iv["calendar_event_id"] == "E1"
    assert one(conn, "SELECT status FROM applications WHERE id=?", aid)[0] == "Rejected"      # never moves backwards
    calsync.dismiss_event(conn, items[1]["id"])
    process(conn, raw_event("E2", "Screening call", "2026-10-15T09:00:00Z", "2026-10-15T09:30:00Z", attendees=["z@else.com"]))
    assert one(conn, "SELECT COUNT(*) FROM review_items WHERE state='open'")[0] == 0           # dismissed events stay dismissed


# ---- auto-complete ----
def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_auto_complete_only_after_the_end_time(conn):
    iid = seeded(conn)    # ends 14:00Z on 13 Oct
    assert calsync.auto_complete(conn, at("2026-10-13T13:59:00")) == 0
    assert calsync.auto_complete(conn, at("2026-10-13T14:00:01")) == 1
    iv = one(conn, "SELECT * FROM interviews WHERE id=?", iid)
    assert iv["status"] == "Completed" and iv["auto_completed"] == 1


def test_undo_restores_scheduled_and_stops_it_completing_again(conn):
    iid = seeded(conn)
    calsync.auto_complete(conn, at("2026-10-14T00:00:00"))
    assert calsync.undo_complete(conn, iid) is True
    assert calsync.auto_complete(conn, at("2026-10-20T00:00:00")) == 0
    assert one(conn, "SELECT status FROM interviews WHERE id=?", iid)[0] == "Scheduled"


def test_undo_does_nothing_for_hand_completed(conn):
    iid = seeded(conn)
    conn.execute("UPDATE interviews SET status='Completed' WHERE id=?", (iid,))
    assert calsync.undo_complete(conn, iid) is False


def test_interviews_without_a_time_are_not_auto_completed(conn):
    run_emails(conn, [fx.ASSESSMENT])
    assert calsync.auto_complete(conn, at("2030-01-01T00:00:00")) == 0
