"""Status rules: automatic changes only move forward."""
import json

import pytest

import fixtures as fx
from helpers import app_row, classification, message, run_emails
from tracker import db
from tracker.appops import recompute
from tracker.engine import Summary, commit_to_application, handle_email
from tracker.statuses import CATEGORY_STATUS, advance


@pytest.mark.parametrize("current, target, expected", [
    ("Applied", "Interviewing", "Interviewing"),
    ("Applied", "Rejected", "Rejected"),
    ("Applied", "Offer", "Offer"),
    ("Interviewing", "Applied", "Interviewing"),       # a later acknowledgement must not reset it
    ("Interviewing", "Rejected", "Rejected"),
    ("Interviewing", "Offer", "Offer"),
    ("Rejected", "Interviewing", "Rejected"),          # never backwards
    ("Rejected", "Applied", "Rejected"),
    ("Offer", "Rejected", "Offer"),                    # same rank: first terminal outcome stays
    ("Rejected", "Offer", "Rejected"),
    ("Withdrawn", "Rejected", "Withdrawn"),            # manual-only: automation never touches it
    ("Withdrawn", "Interviewing", "Withdrawn"),
    ("No response", "Rejected", "Rejected"),           # a late reply still counts
    ("No response", "Interviewing", "Interviewing"),
    ("Applied", None, "Applied"),
])
def test_advance(current, target, expected):
    assert advance(current, target) == expected


def test_category_mapping():
    assert CATEGORY_STATUS["application_confirmation"] == "Applied"
    assert CATEGORY_STATUS["acknowledgement"] == "Applied"
    assert CATEGORY_STATUS["interview_invite"] == "Interviewing"
    assert CATEGORY_STATUS["assessment"] == "Interviewing"
    assert CATEGORY_STATUS["rejection"] == "Rejected"
    assert CATEGORY_STATUS["offer"] == "Offer"
    assert "recruiter_message" not in CATEGORY_STATUS and "interview_cancelled" not in CATEGORY_STATUS


def _status(conn, company):
    return conn.execute("SELECT status FROM applications WHERE company LIKE ?", (f"{company}%",)).fetchone()[0]


def test_confirmation_then_interview_then_late_acknowledgement_stays_interviewing(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION])
    assert _status(conn, "Harlow") == "Applied"
    # an interview for the same job
    iv = dict(fx.INTERVIEW_INVITE_WITH_TIME, received="2026-09-15T09:00:00Z",
              classification=dict(fx.INTERVIEW_INVITE_WITH_TIME["classification"], company="Harlow & Pine", role_title="Business Analyst"))
    run_emails(conn, [iv])
    assert _status(conn, "Harlow") == "Interviewing"
    ack = dict(fx.HARLOW_CONFIRMATION, subject="Still reviewing", received="2026-09-20T09:00:00Z",
               classification=dict(fx.HARLOW_CONFIRMATION["classification"], category="acknowledgement"))
    run_emails(conn, [ack])
    assert _status(conn, "Harlow") == "Interviewing"


def test_rejection_after_confirmation(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    assert _status(conn, "Harlow") == "Rejected"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1   # matched, not duplicated


def test_interview_after_rejection_does_not_reopen(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    late = dict(fx.REJECTION, subject="Actually, an interview?", received="2026-10-05T09:00:00Z",
                classification=dict(fx.REJECTION["classification"], category="interview_invite"))
    run_emails(conn, [late])
    assert _status(conn, "Harlow") == "Rejected"


def test_locked_status_is_never_changed_by_a_sync(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION])
    conn.execute("UPDATE applications SET status='Withdrawn', locked_fields=?", (json.dumps(["status"]),))
    run_emails(conn, [fx.REJECTION])
    assert _status(conn, "Harlow") == "Withdrawn"


def test_applied_on_is_the_first_email_whatever_it_was(conn):
    run_emails(conn, [fx.REJECTION])                       # the only email is a rejection
    row = conn.execute("SELECT applied_at FROM applications").fetchone()
    assert row["applied_at"] == "2026-09-30T10:05:00+00:00"


def test_applied_on_moves_back_if_an_earlier_email_arrives_later(conn):
    run_emails(conn, [fx.REJECTION])
    with db.tx(conn):  # an older confirmation is discovered afterwards
        handle_email(conn, message(fx.HARLOW_CONFIRMATION), "applications", classification(fx.HARLOW_CONFIRMATION), 0.7, Summary())
    assert conn.execute("SELECT applied_at FROM applications").fetchone()[0] == "2026-09-10T09:00:00+00:00"


def test_recompute_rebuilds_status_from_timeline(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    conn.execute("UPDATE applications SET status='Applied', applied_at=NULL")   # corrupt it
    recompute(conn, conn.execute("SELECT id FROM applications").fetchone()[0])
    row = conn.execute("SELECT status, applied_at FROM applications").fetchone()
    assert row["status"] == "Rejected" and row["applied_at"] == "2026-09-10T09:00:00+00:00"
