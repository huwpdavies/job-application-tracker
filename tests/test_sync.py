"""End-to-end sync with a fake Graph and a fake Claude: first run, incremental, dry-run, retries, Inbox, calendar."""
import pytest

import fixtures as fx
from helpers import FakeClassifier, FakeGraph, classification, graph_raw, raw_event
from tracker import db, sync
from tracker.graph import GraphError


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    FakeClassifier.calls, FakeClassifier.answers = [], {}
    monkeypatch.setattr(sync, "Classifier", FakeClassifier)
    for f in fx.ALL + [fx.HARLOW_CONFIRMATION]:
        FakeClassifier.answers[f["subject"]] = classification(f)


def run(cfg, g, conn, **kw):
    lines = []
    kw.setdefault("confirm", lambda p: True)
    s = sync.run(cfg, g, conn, out=lines.append, progress=lambda a, b: None, **kw)
    return s, "\n".join(lines)


def graph_with(*fixes):
    g = FakeGraph()
    g.delta_batches = [[graph_raw(f) for f in fixes]]
    return g


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def test_first_run_asks_for_confirmation_and_cancelling_sends_and_saves_nothing(cfg, conn):
    g = graph_with(*fx.ALL)
    asked = []
    s, out = run(cfg, g, conn, confirm=lambda p: asked.append(p) or False)
    assert s is None and asked and "Estimated Claude usage" in out
    assert FakeClassifier.calls == [] and count(conn, "applications") == 0 and count(conn, "emails") == 0
    assert db.kv_get(conn, "sync_state", "last_sync_at") is None


def test_first_run_processes_everything_and_remembers_state(cfg, conn):
    g = graph_with(*fx.ALL)
    s, out = run(cfg, g, conn)
    assert len(FakeClassifier.calls) == len(fx.ALL)
    assert s.new_applications == 5 and s.interviews_found == 2 and s.other == 1     # Northwind, Northbridge, Harlow, Brightwater, Calder
    assert count(conn, "applications") == 5 and count(conn, "emails") == 7
    assert db.kv_get(conn, "sync_state", "last_sync_at") and db.kv_get(conn, "sync_state", "delta:F1") == "DELTA-1"
    assert db.kv_get(conn, "sync_state", "me_address") == "huw@example.com"
    assert g.delta_calls[0][1] is None and g.delta_calls[0][2]                     # first call: window, no saved link


def test_oldest_first_so_a_rejection_attaches_to_its_earlier_confirmation(cfg, conn):
    g = graph_with(fx.REJECTION, fx.HARLOW_CONFIRMATION)                           # delivered newest-first
    run(cfg, g, conn)
    assert count(conn, "applications") == 1
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "Rejected"


def test_second_run_uses_the_saved_delta_link_and_only_handles_new_mail(cfg, conn):
    g = graph_with(fx.LINKEDIN_CONFIRMATION)
    run(cfg, g, conn)
    FakeClassifier.calls.clear()
    g.delta_batches = [[graph_raw(fx.WORKDAY_CONFIRMATION)]]
    s, _ = run(cfg, g, conn)
    assert g.delta_calls[1][1] == "DELTA-1"                                         # resumed from the stored token
    assert FakeClassifier.calls == [fx.WORKDAY_CONFIRMATION["subject"]]
    assert s.new_applications == 1 and count(conn, "applications") == 2


def test_nothing_is_classified_twice_even_if_delta_repeats_a_message(cfg, conn):
    raw = graph_raw(fx.LINKEDIN_CONFIRMATION, internet_id="<dup@x>")
    g = FakeGraph(); g.delta_batches = [[raw]]
    run(cfg, g, conn)
    FakeClassifier.calls.clear()
    g.delta_batches = [[graph_raw(fx.LINKEDIN_CONFIRMATION, gid="NEWGRAPHID", internet_id="<dup@x>")]]   # same email, new Graph id
    s, _ = run(cfg, g, conn)
    assert FakeClassifier.calls == [] and s.skipped_known == 1 and count(conn, "emails") == 1


def test_dry_run_prints_actions_but_saves_nothing(cfg, conn):
    g = graph_with(*fx.ALL)
    s, out = run(cfg, g, conn, dry_run=True)
    assert "Dry-run summary" in out and "CREATE" in out
    assert s.new_applications == 5
    for table in ("applications", "emails", "events", "interviews", "review_items", "sync_runs"):
        assert count(conn, table) == 0, table
    assert db.kv_get(conn, "sync_state", "delta:F1") is None and db.kv_get(conn, "sync_state", "last_sync_at") is None


def test_one_failing_email_does_not_stop_the_sync_and_is_retried_next_time(cfg, conn):
    FakeClassifier.answers[fx.REJECTION["subject"]] = "API error: overloaded"
    g = graph_with(fx.HARLOW_CONFIRMATION, fx.REJECTION, fx.LINKEDIN_CONFIRMATION)
    s, _ = run(cfg, g, conn)
    assert s.errors == 1 and s.new_applications == 2
    assert count(conn, "emails", "state='error'") == 1
    # the API recovers; delta won't return that email again, but the sync fetches failed emails by id
    FakeClassifier.answers[fx.REJECTION["subject"]] = classification(fx.REJECTION)
    FakeClassifier.calls.clear()
    s2, _ = run(cfg, g, conn)
    assert FakeClassifier.calls == [fx.REJECTION["subject"]]
    assert count(conn, "emails", "state='error'") == 0
    assert conn.execute("SELECT status FROM applications WHERE company LIKE 'Harlow%'").fetchone()[0] == "Rejected"


def test_cached_classifications_cost_nothing_on_a_rebuild(cfg, conn, tmp_path):
    g = graph_with(*fx.ALL)
    run(cfg, g, conn)
    conn2 = db.connect(tmp_path / "second.db")
    FakeClassifier.calls.clear()
    g2 = FakeGraph(); g2.delta_batches = [list(g.by_id.values())]
    s, out = run(cfg, g2, conn2)
    assert FakeClassifier.calls == [] and "No Claude calls needed" in out and s.new_applications == 5


def test_folder_not_found_is_reported(cfg, conn):
    g = graph_with(fx.LINKEDIN_CONFIRMATION)
    g.folder_name = "Something Else"
    s, out = run(cfg, g, conn)
    assert s is None and "Could not find a mail folder" in out


def test_inbox_check_only_looks_at_known_senders_after_the_first_run(cfg, conn):
    g = graph_with(fx.REJECTION, fx.HARLOW_CONFIRMATION)       # known company sender: harlowandpine.co.uk
    run(cfg, g, conn)
    from helpers import graph_raw as raw
    reply = dict(fx.REJECTION, subject="Following up", received="2026-10-04T09:00:00Z", sender="recruitment@harlowandpine.co.uk")
    stranger = dict(fx.NEWSLETTER, subject="Random promo", sender="deals@shop.com")
    jobboard = dict(fx.NEWSLETTER, subject="Job alert", sender="alerts@reed.co.uk")
    g.inbox = [raw(reply, folder="INBOX"), raw(stranger, folder="INBOX"), raw(jobboard, folder="INBOX")]
    for r in g.inbox:
        g.by_id[r["id"]] = r
    FakeClassifier.answers["Following up"] = classification(fx.REJECTION, category="acknowledgement")
    FakeClassifier.calls.clear()
    g.delta_batches = [[]]
    s, out = run(cfg, g, conn)
    assert "Inbox: 1 email(s) from known application senders" in out
    assert FakeClassifier.calls == ["Following up"]
    assert conn.execute("SELECT not_filed_yet FROM emails WHERE subject='Following up'").fetchone()[0] == 1


def test_inbox_check_can_be_switched_off(cfg, conn):
    from tracker import settings
    g = graph_with(fx.REJECTION)
    run(cfg, g, conn)
    settings.save(conn, "inbox_check", False)
    g.inbox = [graph_raw(dict(fx.REJECTION, subject="Another"), folder="INBOX")]
    g.delta_batches = [[]]
    _, out = run(cfg, g, conn)
    assert "Inbox:" not in out


def test_calendar_is_matched_during_sync_and_shows_in_summary(cfg, conn):
    g = graph_with(fx.INTERVIEW_INVITE_WITH_TIME)
    g.events = [raw_event("E1", "Interview with Brightwater Energy", "2026-10-13T13:30:00Z", "2026-10-13T14:15:00Z")]
    s, out = run(cfg, g, conn)
    assert s.calendar_matched == 1 and "calendar matches:      1" in out
    assert conn.execute("SELECT start_at FROM interviews").fetchone()[0] == "2026-10-13T13:30:00+00:00"


def test_calendar_failure_does_not_fail_the_sync(cfg, conn):
    g = graph_with(fx.LINKEDIN_CONFIRMATION)
    g.calendar_error = GraphError("403 Forbidden")
    s, out = run(cfg, g, conn)
    assert s.new_applications == 1 and "Calendar skipped" in out


def test_past_interviews_are_auto_completed_by_the_sync(cfg, conn):
    past = dict(fx.INTERVIEW_INVITE_WITH_TIME, classification=dict(fx.INTERVIEW_INVITE_WITH_TIME["classification"], interview_datetime="2026-01-13T14:00:00+00:00"))
    FakeClassifier.answers[past["subject"]] = classification(past)
    s, _ = run(cfg, graph_with(past), conn)
    assert s.auto_completed == 1 and conn.execute("SELECT status FROM interviews").fetchone()[0] == "Completed"


def test_limit_does_not_advance_the_delta_token(cfg, conn):
    g = graph_with(*fx.ALL)
    run(cfg, g, conn, limit=2)
    assert db.kv_get(conn, "sync_state", "delta:F1") is None and db.kv_get(conn, "sync_state", "last_sync_at") is None
