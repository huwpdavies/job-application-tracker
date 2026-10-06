"""The six sample emails (plus extras) pushed through the real engine, with Claude mocked."""
import json

import fixtures as fx
from helpers import classification, message, run_emails
from tracker import db
from tracker.engine import Summary, find_existing, handle_email, note_moved


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def test_linkedin_confirmation_creates_application(conn):
    s = run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    a = one(conn, "SELECT * FROM applications")
    assert (a["company"], a["role_title"], a["source"], a["status"]) == ("Northwind Analytics", "Senior Data Analyst", "LinkedIn", "Applied")
    assert a["location"] == "Manchester" and a["applied_at"] == "2026-09-22T08:14:00+00:00"
    assert s.new_applications == 1
    ev = one(conn, "SELECT * FROM events")
    assert ev["category"] == "application_confirmation" and ev["web_link"].startswith("https://outlook")


def test_workday_confirmation_keeps_job_reference(conn):
    run_emails(conn, [fx.WORKDAY_CONFIRMATION])
    a = one(conn, "SELECT * FROM applications")
    assert a["job_reference"] == "R-104522" and a["company"] == "Northbridge Insurance Group"


def test_rejection_attaches_to_existing_application_despite_name_differences(conn):
    # confirmation says "Harlow & Pine Ltd", the rejection says "Harlow & Pine"
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    assert one(conn, "SELECT COUNT(*) FROM applications")[0] == 1
    assert one(conn, "SELECT status FROM applications")[0] == "Rejected"
    assert one(conn, "SELECT COUNT(*) FROM events")[0] == 2


def test_rejection_with_no_earlier_email_creates_the_application(conn):
    run_emails(conn, [fx.REJECTION])
    a = one(conn, "SELECT * FROM applications")
    assert a["status"] == "Rejected" and a["company"] == "Harlow & Pine"


def test_interview_with_time_creates_scheduled_interview_in_utc(conn):
    s = run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME])
    iv = one(conn, "SELECT * FROM interviews")
    assert iv["status"] == "Scheduled" and iv["kind"] == "interview"
    assert iv["start_at"] == "2026-10-13T13:00:00+00:00"     # 14:00 BST
    assert iv["end_at"] == "2026-10-13T14:00:00+00:00"       # defaults to one hour
    assert (iv["format"], iv["stage"]) == ("video", "first round")
    assert one(conn, "SELECT status FROM applications")[0] == "Interviewing"
    assert s.interviews_found == 1


def test_reschedule_updates_the_same_interview(conn):
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME, fx.RESCHEDULE])
    rows = conn.execute("SELECT * FROM interviews").fetchall()
    assert len(rows) == 1
    assert rows[0]["start_at"] == "2026-10-15T09:30:00+00:00"      # 10:30 BST
    assert one(conn, "SELECT COUNT(*) FROM applications")[0] == 1


def test_two_times_on_the_same_day_are_one_interview(conn):
    a = dict(fx.INTERVIEW_INVITE_WITH_TIME, received="2026-10-01T09:00:00Z")
    b = dict(fx.INTERVIEW_INVITE_WITH_TIME, received="2026-10-02T09:00:00Z", subject="New time same day",
             classification=dict(fx.INTERVIEW_INVITE_WITH_TIME["classification"], interview_datetime="2026-10-13T16:00:00+01:00"))
    run_emails(conn, [a, b])
    rows = conn.execute("SELECT * FROM interviews").fetchall()
    assert len(rows) == 1 and rows[0]["start_at"] == "2026-10-13T15:00:00+00:00"


def test_invite_without_time_is_new_then_scheduled_when_time_arrives(conn):
    invite = dict(fx.INTERVIEW_INVITE_WITH_TIME, received="2026-10-01T09:00:00Z",
                  classification=dict(fx.INTERVIEW_INVITE_WITH_TIME["classification"], category="interview_invite", interview_datetime=None))
    run_emails(conn, [invite])
    assert one(conn, "SELECT status FROM interviews")[0] == "New"
    run_emails(conn, [fx.RESCHEDULE])      # a time turns up
    rows = conn.execute("SELECT * FROM interviews").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "Scheduled"


def test_cancellation_marks_the_interview_cancelled(conn):
    cancel = dict(fx.RESCHEDULE, received="2026-10-09T09:00:00Z", subject="Cancelled",
                  classification=dict(fx.RESCHEDULE["classification"], category="interview_cancelled", interview_datetime=None))
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME, cancel])
    assert one(conn, "SELECT status FROM interviews")[0] == "Cancelled"


def test_assessment_becomes_a_dateless_interview_item(conn):
    run_emails(conn, [fx.ASSESSMENT])
    iv = one(conn, "SELECT * FROM interviews")
    assert iv["kind"] == "assessment" and iv["status"] == "New" and iv["start_at"] is None
    assert one(conn, "SELECT status FROM applications")[0] == "Interviewing"


def test_assessment_reminder_does_not_duplicate(conn):
    reminder = dict(fx.ASSESSMENT, subject="Reminder: assessment", received="2026-10-05T09:00:00Z")
    run_emails(conn, [fx.ASSESSMENT, reminder])
    assert one(conn, "SELECT COUNT(*) FROM interviews")[0] == 1
    assert one(conn, "SELECT COUNT(*) FROM events")[0] == 2


def test_assessment_and_interview_are_separate_items(conn):
    a = dict(fx.ASSESSMENT, classification=dict(fx.ASSESSMENT["classification"], company="Brightwater Energy", role_title="Reporting Analyst"))
    run_emails(conn, [a, fx.INTERVIEW_INVITE_WITH_TIME])
    kinds = sorted(r["kind"] for r in conn.execute("SELECT kind FROM interviews"))
    assert kinds == ["assessment", "interview"]


def test_newsletter_is_stored_but_creates_nothing(conn):
    s = run_emails(conn, [fx.NEWSLETTER])
    assert one(conn, "SELECT COUNT(*) FROM applications")[0] == 0
    assert one(conn, "SELECT COUNT(*) FROM emails")[0] == 1 and s.other == 1


def test_low_confidence_goes_to_review_without_touching_applications(conn):
    s = run_emails(conn, [fx.LINKEDIN_CONFIRMATION], confidence=0.55)
    assert one(conn, "SELECT COUNT(*) FROM applications")[0] == 0
    ri = one(conn, "SELECT * FROM review_items")
    assert "Low confidence" in ri["reason"] and s.needs_review == 1
    assert one(conn, "SELECT state FROM emails")[0] == "review"


def test_threshold_is_configurable(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION], threshold=0.99)
    assert one(conn, "SELECT COUNT(*) FROM review_items")[0] == 1


def test_missing_company_goes_to_review(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION], company=None)
    assert "Company not identified" in one(conn, "SELECT reason FROM review_items")[0]


def test_recruiter_message_with_no_application_goes_to_review(conn):
    run_emails(conn, [fx.REJECTION], category="recruiter_message")
    assert one(conn, "SELECT COUNT(*) FROM applications")[0] == 0
    assert one(conn, "SELECT COUNT(*) FROM review_items")[0] == 1


def test_ambiguous_company_goes_to_review(conn):
    a = dict(fx.HARLOW_CONFIRMATION, received="2026-09-01T09:00:00Z")
    b = dict(fx.HARLOW_CONFIRMATION, subject="Other role", received="2026-09-02T09:00:00Z",
             classification=dict(fx.HARLOW_CONFIRMATION["classification"], role_title="Data Engineer"))
    run_emails(conn, [a, b])
    run_emails(conn, [fx.REJECTION], role_title=None)
    assert "Several applications" in one(conn, "SELECT reason FROM review_items")[0]


def test_hand_edited_fields_are_not_overwritten_by_later_syncs(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    conn.execute("UPDATE applications SET location='Remote (my edit)', status='Withdrawn', locked_fields=?", (json.dumps(["location", "status"]),))
    follow = dict(fx.LINKEDIN_CONFIRMATION, subject="Update", received="2026-09-25T09:00:00Z",
                  classification=dict(fx.LINKEDIN_CONFIRMATION["classification"], category="rejection", location="Manchester HQ"))
    run_emails(conn, [follow])
    a = one(conn, "SELECT * FROM applications")
    assert a["location"] == "Remote (my edit)" and a["status"] == "Withdrawn"


def test_gaps_are_filled_but_existing_values_kept(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    follow = dict(fx.LINKEDIN_CONFIRMATION, subject="Update", received="2026-09-25T09:00:00Z",
                  classification=dict(fx.LINKEDIN_CONFIRMATION["classification"], category="acknowledgement", location="Elsewhere", job_reference="REF-1"))
    run_emails(conn, [follow])
    a = one(conn, "SELECT * FROM applications")
    assert a["location"] == "Manchester" and a["job_reference"] == "REF-1"


def test_follow_up_clock_counts_inbound_mail_only(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    mine = dict(fx.LINKEDIN_CONFIRMATION, sender="huw@example.com", subject="My chaser", received="2026-10-01T09:00:00Z",
                classification=dict(fx.LINKEDIN_CONFIRMATION["classification"], category="acknowledgement"))
    with db.tx(conn):
        handle_email(conn, message(mine), "applications", classification(mine), 0.7, Summary(), me_address="huw@example.com")
    a = one(conn, "SELECT * FROM applications")
    assert a["last_activity_at"] == "2026-10-01T09:00:00+00:00"
    assert a["last_inbound_at"] == "2026-09-22T08:14:00+00:00"       # my own email didn't reset the clock


def test_classification_failure_is_recorded_not_raised(conn):
    summary = Summary()
    with db.tx(conn):
        handle_email(conn, message(fx.LINKEDIN_CONFIRMATION), "applications", None, 0.7, summary, error="API error: boom")
    assert one(conn, "SELECT state FROM emails")[0] == "error" and summary.errors == 1


def test_moved_message_is_recognised_by_internet_message_id(conn):
    inbox_copy = message(fx.LINKEDIN_CONFIRMATION, gid="INBOX-ID", internet_id="<same@x>")
    with db.tx(conn):
        handle_email(conn, inbox_copy, "inbox", classification(fx.LINKEDIN_CONFIRMATION), 0.7, Summary())
    assert one(conn, "SELECT not_filed_yet FROM emails")[0] == 1
    moved = message(fx.LINKEDIN_CONFIRMATION, gid="FOLDER-ID", internet_id="<same@x>")   # moved: new Graph id
    existing = find_existing(conn, moved.internet_message_id, moved.graph_id)
    assert existing is not None                                     # => not classified a second time
    with db.tx(conn):
        note_moved(conn, existing, moved, "applications")
    row = one(conn, "SELECT * FROM emails")
    assert (row["graph_id"], row["folder"], row["not_filed_yet"]) == ("FOLDER-ID", "applications", 0)
    assert one(conn, "SELECT COUNT(*) FROM emails")[0] == 1
