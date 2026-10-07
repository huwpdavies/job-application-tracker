"""Google Calendar: pre-filled link, .ics file, eligibility, the web actions, and the v3 -> v4 migration."""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import fixtures as fx
from helpers import run_emails
from tracker import db, gcal
from tracker.web.app import create_app


def rows(conn):
    iv = conn.execute("SELECT * FROM interviews").fetchone()
    app = conn.execute("SELECT * FROM applications WHERE id=?", (iv["application_id"],)).fetchone()
    return iv, app


@pytest.fixture
def scheduled(conn):
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME])       # 13 Oct 2026, 14:00 BST = 13:00Z, video, first round
    conn.execute("UPDATE applications SET job_url='https://example.com/jobs/42'")
    return rows(conn)


def test_google_link_is_a_prefilled_new_event_in_utc(scheduled):
    iv, app = scheduled
    url = urlparse(gcal.google_url(iv, app))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.scheme, url.netloc, url.path) == ("https", "calendar.google.com", "/calendar/render")
    assert q["action"] == "TEMPLATE"
    assert q["text"] == "Interview: Reporting Analyst at Brightwater Energy"
    assert q["dates"] == "20261013T130000Z/20261013T140000Z"           # stored UTC, one hour default
    assert q["ctz"] == "Europe/London" and q["location"] == "Video call"
    assert "Stage: first round" in q["details"] and "Job ad: https://example.com/jobs/42" in q["details"]


def test_event_contains_no_email_content(scheduled):
    iv, app = scheduled
    q = parse_qs(urlparse(gcal.google_url(iv, app)).query)
    everything = " ".join(v[0] for v in q.values())
    assert "Jessica" not in everything and "Teams" not in everything and "First-round video interview on" not in everything


def test_end_time_comes_from_the_interview_when_known(conn, scheduled):
    conn.execute("UPDATE interviews SET end_at='2026-10-13T13:30:00+00:00'")
    iv, app = rows(conn)
    assert "dates=20261013T130000Z%2F20261013T133000Z" in gcal.google_url(iv, app)


def test_role_is_optional_in_the_title(conn, scheduled):
    conn.execute("UPDATE applications SET role_title=''")
    iv, app = rows(conn)
    assert gcal.event_fields(iv, app)["title"] == "Interview at Brightwater Energy"


@pytest.mark.parametrize("changes", [
    "status='New', start_at=NULL, end_at=NULL",
    "status='Cancelled'",
    "status='Completed'",
    "kind='assessment'",
])
def test_only_scheduled_interviews_can_become_events(conn, scheduled, changes):
    conn.execute(f"UPDATE interviews SET {changes}")
    iv, app = rows(conn)
    with pytest.raises(gcal.NotEligible):
        gcal.google_url(iv, app)
    with pytest.raises(gcal.NotEligible):
        gcal.ics(iv, app)


def test_ics_is_a_valid_looking_calendar_file(scheduled):
    iv, app = scheduled
    text = gcal.ics(iv, app, now=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\r\nDTSTART:20261013T130000Z\r\n" in text and "\r\nDTEND:20261013T140000Z\r\n" in text
    assert f"\r\nUID:interview-{iv['id']}@job-tracker.local\r\n" in text and "\r\nDTSTAMP:20261007T090000Z\r\n" in text
    assert "SUMMARY:Interview: Reporting Analyst at Brightwater Energy" in text and "LOCATION:Video call" in text
    assert text.count("\n") == text.count("\r\n")                      # CRLF throughout
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))


def test_ics_escapes_special_characters(conn, scheduled):
    conn.execute("UPDATE applications SET company='Smith, Jones; & Co', job_url='https://x.io/a,b'")
    iv, app = rows(conn)
    text = gcal.ics(iv, app)
    assert "Smith\\, Jones\\; & Co" in text.replace("\r\n ", "")        # unfold before checking
    assert "\\n" in text and "\n " not in text.replace("\r\n ", "")     # newlines in the description are escaped, not literal


def test_long_lines_are_folded_and_unfold_back(conn, scheduled):
    conn.execute("UPDATE applications SET job_url=?", ("https://example.com/" + "x" * 200,))
    iv, app = rows(conn)
    text = gcal.ics(iv, app)
    assert all(len(l.encode()) <= 75 for l in text.split("\r\n"))
    assert "x" * 200 in text.replace("\r\n ", "")


# ---- web ----
@pytest.fixture
def client(cfg, scheduled):
    with TestClient(create_app(cfg), follow_redirects=False) as c:
        yield c


def test_button_posts_then_redirects_to_google_and_remembers(client, conn, scheduled):
    iv, _ = scheduled
    assert "Add to Google Calendar" in client.get("/interviews").text
    r = client.post(f"/interviews/{iv['id']}/google")
    assert r.status_code == 303 and r.headers["location"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert conn.execute("SELECT google_added_at FROM interviews").fetchone()[0]
    assert "opened" in client.get("/interviews").text and "Add to Google again" in client.get("/interviews").text


def test_buttons_also_appear_on_the_application_and_overview_pages(client, conn, scheduled):
    iv, app = scheduled
    assert "Add to Google Calendar" in client.get(f"/applications/{app['id']}").text
    conn.execute("UPDATE interviews SET start_at=?, end_at=?", ("2099-01-01T10:00:00+00:00", "2099-01-01T11:00:00+00:00"))
    # upcoming (next 14 days) only lists near-term interviews; make it near-term
    soon = datetime.now(timezone.utc).replace(microsecond=0)
    conn.execute("UPDATE interviews SET start_at=?, end_at=?", ((soon.replace(hour=23, minute=0, second=0)).isoformat(), (soon.replace(hour=23, minute=59, second=0)).isoformat()))
    assert "Add to Google Calendar" in client.get("/").text


def test_no_button_for_assessments_or_unscheduled(client, conn, scheduled):
    conn.execute("UPDATE interviews SET status='New', start_at=NULL, end_at=NULL")
    assert "Add to Google Calendar" not in client.get("/interviews").text
    iv, _ = scheduled
    r = client.post(f"/interviews/{iv['id']}/google")
    assert r.status_code == 303 and "err=" in r.headers["location"] and "calendar.google.com" not in r.headers["location"]


def test_ics_download(client, scheduled):
    iv, _ = scheduled
    r = client.get(f"/interviews/{iv['id']}/calendar.ics")
    assert r.headers["content-type"].startswith("text/calendar") and "attachment" in r.headers["content-disposition"]
    assert "BEGIN:VEVENT" in r.text
    assert client.get("/interviews/9999/calendar.ics").status_code == 404


def test_cross_site_post_to_the_google_route_is_refused(client, scheduled):
    iv, _ = scheduled
    assert client.post(f"/interviews/{iv['id']}/google", headers={"Origin": "https://evil.example"}).status_code == 403


# ---- migration ----
def test_v3_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.execute("ALTER TABLE interviews DROP COLUMN google_added_at")      # recreate a v3 database
    c.execute("PRAGMA user_version=3")
    now = db.utcnow()
    c.execute("INSERT INTO applications (company, company_norm, status, created_at, updated_at) VALUES ('Acme','acme','Applied',?,?)", (now, now))
    c.execute("INSERT INTO interviews (application_id, status, created_at, updated_at) VALUES (1,'New',?,?)", (now, now))
    c.close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 4
    assert c.execute("SELECT google_added_at FROM interviews").fetchone()[0] is None     # column exists, existing row kept
    assert c.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
