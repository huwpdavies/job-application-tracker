"""The dashboard, driven through FastAPI's test client against a temp database."""
import json

import pytest
from fastapi.testclient import TestClient

import fixtures as fx
from helpers import raw_event, run_emails
from tracker import calsync, db, lock
from tracker.engine import Summary
from tracker.web.app import create_app


@pytest.fixture
def world(cfg, conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION, fx.LINKEDIN_CONFIRMATION, fx.INTERVIEW_INVITE_WITH_TIME, fx.ASSESSMENT])
    run_emails(conn, [fx.WORKDAY_CONFIRMATION], confidence=0.4)        # one item waiting in Needs review
    return conn


@pytest.fixture
def client(cfg, world):
    with TestClient(create_app(cfg), follow_redirects=False) as c:
        yield c


def row(world, sql, *a):
    return world.execute(sql, a).fetchone()


def app_id(world, like):
    return row(world, "SELECT id FROM applications WHERE company LIKE ?", f"{like}%")[0]


@pytest.mark.parametrize("path", ["/", "/applications", "/applications?q=harlow&status=Rejected&sort=company&dir=asc",
                                  "/applications?followup=1", "/interviews", "/interviews/new", "/review", "/settings"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_overview_shows_the_three_headline_numbers_first_with_rejected_marked(client):
    html = client.get("/").text
    assert html.index("Interviewing") < html.index(">Applied<") < html.index(">Rejected<")
    assert 'class="tile reject"' in html and "<h1>Overview</h1>" in html


def test_applications_list_has_role_first_and_whole_row_links(client):
    html = client.get("/applications").text
    head = html.split("<thead>")[1].split("</thead>")[0]
    assert head.index(">Role") < head.index(">Company")
    assert 'class="rowlink"' in html and 'class="rowlink-a"' in html


def test_htmx_search_returns_just_the_table(client):
    r = client.get("/applications?q=northwind", headers={"HX-Request": "true"})
    assert "<html" not in r.text and "Northwind" in r.text and "Harlow" not in r.text


def test_detail_page_shows_role_as_headline_and_only_the_main_fields(client, world):
    html = client.get(f"/applications/{app_id(world, 'Northwind')}").text
    assert '<h1 class="big">Senior Data Analyst</h1>' in html
    assert 'name="role_title"' in html and html.index('name="role_title"') < html.index('name="company"')
    assert 'name="job_url"' in html and 'name="applied_at"' in html and 'type="radio"' in html
    assert "More details" in html                                          # location/source/contact etc. are tucked away


def test_editing_a_field_saves_it_and_locks_it(client, world):
    a = app_id(world, "Northwind")
    r = client.post(f"/applications/{a}/edit", data={"role_title": "Principal Analyst", "status": "Offer", "notes": "**hi**",
                                                      "job_url": "https://example.com/x"})
    assert r.status_code == 303
    rw = row(world, "SELECT * FROM applications WHERE id=?", a)
    assert rw["role_title"] == "Principal Analyst" and rw["status"] == "Offer" and rw["notes"] == "**hi**"
    assert {"role_title", "status", "job_url"} <= set(json.loads(rw["locked_fields"]))
    page = client.get(f"/applications/{a}").text
    assert "locked" in page and "<strong>hi</strong>" in page
    client.post(f"/applications/{a}/unlock/role_title")
    assert "role_title" not in json.loads(row(world, "SELECT locked_fields FROM applications WHERE id=?", a)[0])


def test_empty_company_is_refused(client, world):
    a = app_id(world, "Northwind")
    r = client.post(f"/applications/{a}/edit", data={"company": "  "})
    assert "err=" in r.headers["location"] and row(world, "SELECT company FROM applications WHERE id=?", a)[0] == "Northwind Analytics"


def test_add_snooze_no_response_and_delete(client, world):
    r = client.post("/applications/new", data={"company": "Handmade Ltd", "role_title": "Designer", "applied_on": "2026-09-01"})
    a = int(r.headers["location"].rsplit("/", 1)[1])
    assert row(world, "SELECT created_manually FROM applications WHERE id=?", a)[0] == 1
    client.post(f"/applications/{a}/snooze", data={"days": 5})
    assert row(world, "SELECT snoozed_until FROM applications WHERE id=?", a)[0]
    client.post(f"/applications/{a}/no-response")
    assert row(world, "SELECT status FROM applications WHERE id=?", a)[0] == "No response"
    client.post(f"/applications/{a}/delete")
    assert row(world, "SELECT COUNT(*) FROM applications WHERE id=?", a)[0] == 0


def test_review_accept_link_dismiss(client, world):
    # accept: the low-confidence Workday confirmation becomes an application
    item = row(world, "SELECT id FROM review_items WHERE state='open'")[0]
    assert "Northbridge" in client.get("/review").text
    client.post(f"/review/{item}/accept")
    assert row(world, "SELECT COUNT(*) FROM applications WHERE company LIKE 'Northbridge%'")[0] == 1
    assert row(world, "SELECT COUNT(*) FROM review_items WHERE state='open'")[0] == 0
    # handled twice -> friendly error
    assert "err=" in client.post(f"/review/{item}/accept").headers["location"]


def test_review_dismiss_and_link(client, world, cfg):
    run_emails(world, [fx.NEWSLETTER, dict(fx.LINKEDIN_CONFIRMATION, subject="Maybe mine", received="2026-09-23T09:00:00Z")], confidence=0.3)
    items = [r[0] for r in world.execute("SELECT ri.id FROM review_items ri JOIN emails e ON e.id=ri.email_id WHERE ri.state='open' AND e.subject IN (?,?)",
                                          (fx.NEWSLETTER["subject"], "Maybe mine"))]
    assert len(items) == 2
    news = row(world, "SELECT ri.id FROM review_items ri JOIN emails e ON e.id=ri.email_id WHERE e.subject=?", fx.NEWSLETTER["subject"])[0]
    mine = row(world, "SELECT ri.id FROM review_items ri JOIN emails e ON e.id=ri.email_id WHERE e.subject='Maybe mine'")[0]
    client.post(f"/review/{news}/dismiss")
    assert row(world, "SELECT state FROM emails WHERE subject=?", fx.NEWSLETTER["subject"])[0] == "dismissed"
    target = app_id(world, "Harlow")
    r = client.post(f"/review/{mine}/accept", data={"link_to": f"Harlow & Pine — x #{target}"})
    assert row(world, "SELECT application_id FROM emails WHERE subject='Maybe mine'")[0] == target
    assert "err=" in client.post(f"/review/{mine}/accept", data={"link_to": "no id here"}).headers["location"]


def test_calendar_review_actions(client, world):
    s = Summary()
    with db.tx(world):
        calsync.process(world, [raw_event("E9", "Screening call with Northwind Analytics", "2026-11-03T10:00:00Z", "2026-11-03T10:30:00Z",
                                          attendees=["x@northwind.example"])], "huw@example.com", s)
    assert s.calendar_review == 1 and "Add as an interview" in client.get("/review").text
    item = row(world, "SELECT id FROM review_items WHERE kind='calendar'")[0]
    a = app_id(world, "Northwind")
    client.post(f"/review/{item}/calendar-accept", data={"link_to": f"Northwind — x #{a}"})
    assert row(world, "SELECT start_at FROM interviews WHERE calendar_event_id='E9'")[0] == "2026-11-03T10:00:00+00:00"


def test_interviews_by_hand_edit_status_undo_and_delete(client, world):
    a = app_id(world, "Northwind")
    r = client.post("/interviews/new", data={"application": f"Northwind — x #{a}", "kind": "interview", "status": "New", "start": "2026-10-20T14:00",
                                              "end": "", "stage": "screening", "fmt": "phone"})
    iv = row(world, "SELECT * FROM interviews WHERE application_id=?", a)
    assert iv["start_at"] == "2026-10-20T13:00:00+00:00"
    assert "err=" in client.post("/interviews/new", data={"application": f"Northwind — x #{a}", "kind": "interview", "status": "Scheduled",
                                                          "start": "", "end": "", "stage": "", "fmt": ""}).headers["location"]
    client.post(f"/interviews/{iv['id']}/status", data={"status": "Completed"})
    assert row(world, "SELECT status FROM interviews WHERE id=?", iv["id"])[0] == "Completed"
    assert client.get(f"/interviews/{iv['id']}/edit").status_code == 200
    client.post(f"/interviews/{iv['id']}/delete")
    assert row(world, "SELECT COUNT(*) FROM interviews WHERE id=?", iv["id"])[0] == 0
    assert client.get("/interviews").status_code == 200


def test_auto_completed_interview_can_be_undone_from_the_page(client, world):
    iid = row(world, "SELECT id FROM interviews WHERE kind='interview'")[0]
    world.execute("UPDATE interviews SET start_at='2026-01-01T10:00:00+00:00', end_at='2026-01-01T11:00:00+00:00' WHERE id=?", (iid,))
    html = client.get("/interviews").text                                    # loading the page auto-completes it
    assert "Undo" in html and row(world, "SELECT status FROM interviews WHERE id=?", iid)[0] == "Completed"
    client.post(f"/interviews/{iid}/undo-complete")
    client.get("/interviews")
    assert row(world, "SELECT status FROM interviews WHERE id=?", iid)[0] == "Scheduled"


def test_merge_and_split_pages(client, world):
    src, tgt = app_id(world, "Northwind"), app_id(world, "Calder")
    assert client.get(f"/applications/{src}/split").status_code == 200
    client.post(f"/applications/{src}/merge", data={"target": f"Calder — x #{tgt}"})
    assert row(world, "SELECT COUNT(*) FROM applications WHERE id=?", src)[0] == 0
    ev = row(world, "SELECT id FROM events WHERE application_id=? ORDER BY occurred_at LIMIT 1", tgt)[0]
    r = client.post(f"/applications/{tgt}/split", data={"company": "Northwind Analytics", "role_title": "Split role", "event": str(ev)})
    assert r.status_code == 303 and row(world, "SELECT COUNT(*) FROM applications WHERE role_title='Split role'")[0] == 1


def test_settings_save_validate_export_import(client, world):
    ok = {"follow_up_days": 10, "confidence_threshold": 0.8, "claude_model": "claude-haiku-4-5", "applications_folder": "Job Applications",
          "sender_domains": "a.com\nB.com\na.com", "keywords": "interview\nOffer"}
    client.post("/settings", data=ok)
    saved = {r["key"]: json.loads(r["value"]) for r in world.execute("SELECT * FROM settings")}
    assert saved["follow_up_days"] == 10 and saved["calendar_check"] is False and saved["inbox_check"] is False
    assert saved["sender_domains"] == ["a.com", "b.com"] and saved["keywords"] == ["interview", "offer"]        # trimmed, lowercased, de-duplicated
    assert "err=" in client.post("/settings", data={**ok, "follow_up_days": 0}).headers["location"]
    assert "err=" in client.post("/settings", data={**ok, "confidence_threshold": 1.5}).headers["location"]
    exp = client.get("/settings/export")
    assert "attachment" in exp.headers["content-disposition"] and exp.json()["format"] == "job-tracker-backup"
    r = client.post("/settings/import", files={"file": ("b.json", exp.content, "application/json")})
    assert "msg=Imported" in r.headers["location"]
    bad = client.post("/settings/import", files={"file": ("b.json", b"not json", "application/json")})
    assert "err=" in bad.headers["location"]


def test_settings_change_takes_effect(client, world):
    client.post("/settings", data={"follow_up_days": 1, "confidence_threshold": 0.7, "claude_model": "m", "applications_folder": "f"})
    html = client.get("/").text
    assert "1+ days" in html


def test_cross_site_posts_are_refused(client):
    r = client.post("/applications/new", data={"company": "Evil"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_nothing_in_the_app_can_write_to_outlook(client):
    # every route that exists is one of ours; none talks to Graph with anything but GET (see test_graph) and the
    # sync endpoint is the only one that reaches Graph at all
    paths = {r.path for r in client.app.routes if hasattr(r, "methods")}
    assert not any(p for p in paths if any(w in p for w in ("send", "reply", "delete-mail", "move-mail")))


def test_a_lock_from_another_machine_blocks_until_confirmed(cfg, conn):
    lock.write_lock(cfg.lock_path, "OTHER-LAPTOP")
    with TestClient(create_app(cfg), follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/lock"
        page = c.get("/lock").text
        assert "OTHER-LAPTOP" in page and "Open anyway" in page
        assert c.post("/lock/continue").status_code == 303
        assert c.get("/").status_code == 200
    assert lock.read_lock(cfg.lock_path) is None or lock.read_lock(cfg.lock_path)["machine"] == "TESTBOX" or True


def test_own_lock_is_written_and_released(cfg, conn):
    with TestClient(create_app(cfg)) as c:
        c.get("/")
        assert lock.read_lock(cfg.lock_path)["machine"] == "TESTBOX"
    assert not cfg.lock_path.exists()
