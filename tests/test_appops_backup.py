"""Manual operations (merge, split, interviews by hand) and the export/import backup."""
import json

import pytest

import fixtures as fx
from helpers import run_emails
from tracker import appops, backup, db, review as review_ops


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def app_id(conn, like):
    return one(conn, "SELECT id FROM applications WHERE company LIKE ?", f"{like}%")[0]


# ---- merge ----
def test_merge_moves_everything_and_deletes_the_source(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION, fx.WORKDAY_CONFIRMATION, fx.ASSESSMENT])
    src, tgt = app_id(conn, "Northwind"), app_id(conn, "Calder")
    conn.execute("UPDATE applications SET notes='remember this' WHERE id=?", (src,))
    appops.merge(conn, src, tgt)
    assert one(conn, "SELECT COUNT(*) FROM applications WHERE id=?", src)[0] == 0
    assert one(conn, "SELECT COUNT(*) FROM events WHERE application_id=?", tgt)[0] == 2
    assert one(conn, "SELECT COUNT(*) FROM emails WHERE application_id=?", tgt)[0] == 2
    t = one(conn, "SELECT * FROM applications WHERE id=?", tgt)
    assert "remember this" in t["notes"] and t["location"] == "Manchester"          # empty fields filled from the source
    assert t["status"] == "Interviewing" and t["applied_at"] == "2026-09-22T08:14:00+00:00"   # recomputed from the combined timeline


def test_merge_into_itself_is_refused(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    a = app_id(conn, "Northwind")
    with pytest.raises(appops.OpError):
        appops.merge(conn, a, a)


# ---- split ----
def test_split_moves_chosen_events_and_recomputes_both_sides(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    a = app_id(conn, "Harlow")
    rej = one(conn, "SELECT id FROM events WHERE category='rejection'")[0]
    new = appops.split(conn, a, [rej], [], "Harlow & Pine", "Data Engineer")
    assert one(conn, "SELECT status FROM applications WHERE id=?", a)[0] == "Applied"       # rejection moved away
    n = one(conn, "SELECT * FROM applications WHERE id=?", new)
    assert n["status"] == "Rejected" and n["role_title"] == "Data Engineer"
    assert "company" in json.loads(n["locked_fields"])
    assert one(conn, "SELECT application_id FROM emails WHERE category='rejection'")[0] == new


def test_split_refuses_empty_or_total_moves(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION])
    a = app_id(conn, "Harlow")
    ids = [r[0] for r in conn.execute("SELECT id FROM events")]
    with pytest.raises(appops.OpError):
        appops.split(conn, a, [], [], "X", "")
    with pytest.raises(appops.OpError):
        appops.split(conn, a, ids, [], "X", "")


def test_split_takes_email_linked_interviews_with_their_emails(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION])
    iv = dict(fx.INTERVIEW_INVITE_WITH_TIME, received="2026-09-15T09:00:00Z",
              classification=dict(fx.INTERVIEW_INVITE_WITH_TIME["classification"], company="Harlow & Pine", role_title="Business Analyst"))
    run_emails(conn, [iv])
    a = app_id(conn, "Harlow")
    ev = one(conn, "SELECT id FROM events WHERE category='interview_scheduled'")[0]
    new = appops.split(conn, a, [ev], [], "Harlow & Pine", "Other")
    assert one(conn, "SELECT application_id FROM interviews")[0] == new


# ---- interviews by hand ----
def test_manual_interview_uk_time_is_stored_as_utc_with_default_end(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    a = app_id(conn, "Northwind")
    iid = appops.create_interview(conn, a, "interview", "New", "2026-10-20T14:00", "", "screening", "phone")
    iv = one(conn, "SELECT * FROM interviews WHERE id=?", iid)
    assert (iv["start_at"], iv["end_at"], iv["status"]) == ("2026-10-20T13:00:00+00:00", "2026-10-20T14:00:00+00:00", "Scheduled")
    assert iv["created_manually"] == 1 and "start_at" in json.loads(iv["locked_fields"])
    assert one(conn, "SELECT status FROM applications WHERE id=?", a)[0] == "Interviewing"


@pytest.mark.parametrize("kw", [
    dict(status="Scheduled", start="", end=""),                              # scheduled needs a time
    dict(status="New", start="2026-10-20T14:00", end="2026-10-20T13:00"),    # end before start
    dict(status="Bogus", start="", end=""),
    dict(status="New", start="soon", end=""),
])
def test_manual_interview_validation(conn, kw):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    with pytest.raises(appops.OpError):
        appops.create_interview(conn, app_id(conn, "Northwind"), "interview", kw["status"], kw["start"], kw["end"], "", "")


def test_assessment_by_hand_needs_no_date(conn):
    run_emails(conn, [fx.LINKEDIN_CONFIRMATION])
    iid = appops.create_interview(conn, app_id(conn, "Northwind"), "assessment", "New", "", "", "", "")
    assert one(conn, "SELECT status FROM interviews WHERE id=?", iid)[0] == "New"


def test_edit_locks_changed_fields_and_delete_works(conn):
    run_emails(conn, [fx.INTERVIEW_INVITE_WITH_TIME])
    iid = one(conn, "SELECT id FROM interviews")[0]
    appops.update_interview(conn, iid, "interview", "Scheduled", "2026-10-14T09:00", "2026-10-14T09:30", "final", "onsite")
    iv = one(conn, "SELECT * FROM interviews WHERE id=?", iid)
    assert iv["start_at"] == "2026-10-14T08:00:00+00:00" and {"start_at", "stage", "format"} <= set(json.loads(iv["locked_fields"]))
    appops.delete_interview(conn, iid)
    assert one(conn, "SELECT COUNT(*) FROM interviews")[0] == 0


# ---- backup ----
def align_ids(src, dst):
    """A real rebuild sees the same emails (same internet message ids); the fixture helper numbers them afresh per run."""
    for r in src.execute("SELECT subject, internet_message_id FROM emails"):
        dst.execute("UPDATE emails SET internet_message_id=? WHERE subject=?", (r["internet_message_id"], r["subject"]))


def make_world(conn):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION, fx.LINKEDIN_CONFIRMATION, fx.ASSESSMENT])


def test_export_contains_only_what_is_yours(conn):
    make_world(conn)
    assert backup.export_data(conn)["applications"] == []                   # nothing hand-edited yet
    conn.execute("UPDATE applications SET notes='mine' WHERE company LIKE 'Northwind%'")
    data = backup.export_data(conn)
    assert [a["notes"] for a in data["applications"]] == ["mine"]
    assert data["format"] == "job-tracker-backup" and data["structure"]


def test_round_trip_into_a_rebuilt_database(conn, tmp_path):
    make_world(conn)
    a = app_id(conn, "Northwind")
    conn.execute("UPDATE applications SET notes='Spoke to **Sam**', contact_name='Sam', role_title='Principal Analyst', "
                 "locked_fields=? WHERE id=?", (json.dumps(["role_title"]), a))
    data = json.loads(json.dumps(backup.export_data(conn)))

    fresh = db.connect(tmp_path / "fresh.db")
    make_world(fresh)                                                        # "rebuilt from email"
    align_ids(conn, fresh)
    assert one(fresh, "SELECT role_title FROM applications WHERE company LIKE 'Northwind%'")[0] == "Senior Data Analyst"
    n = backup.import_data(fresh, data)
    row = one(fresh, "SELECT * FROM applications WHERE company LIKE 'Northwind%'")
    assert row["notes"] == "Spoke to **Sam**" and row["contact_name"] == "Sam"
    assert row["role_title"] == "Principal Analyst" and "role_title" in json.loads(row["locked_fields"])   # renamed apps are still found
    assert n["applications_updated"] == 1


def test_manually_created_applications_and_interviews_are_recreated(conn, tmp_path):
    make_world(conn)
    now = db.utcnow()
    cur = conn.execute("INSERT INTO applications (company, company_norm, role_title, role_norm, status, created_manually, locked_fields, created_at, updated_at) "
                       "VALUES ('Handmade','handmade','Designer','designer','Applied',1,'[]',?,?)", (now, now))
    appops.create_interview(conn, cur.lastrowid, "interview", "New", "2026-11-02T10:00", "", "", "")
    data = json.loads(json.dumps(backup.export_data(conn)))
    fresh = db.connect(tmp_path / "fresh.db")
    make_world(fresh)
    align_ids(conn, fresh)
    n = backup.import_data(fresh, data)
    assert n["applications_created"] == 1 and n["interviews"] == 1
    assert one(fresh, "SELECT COUNT(*) FROM interviews i JOIN applications a ON a.id=i.application_id WHERE a.company='Handmade'")[0] == 1


def test_review_decisions_are_replayed(conn, tmp_path):
    run_emails(conn, [fx.NEWSLETTER, fx.LINKEDIN_CONFIRMATION], confidence=0.5)             # both land in review
    ids = [r[0] for r in conn.execute("SELECT id FROM review_items ORDER BY id")]
    review_ops.dismiss(conn, ids[0])
    data = json.loads(json.dumps(backup.export_data(conn)))
    assert len(data["review_decisions"]) == 1

    fresh = db.connect(tmp_path / "fresh.db")
    run_emails(fresh, [fx.NEWSLETTER, fx.LINKEDIN_CONFIRMATION], confidence=0.5)
    # same messages need the same internet ids across the two databases
    for table_row in conn.execute("SELECT subject, internet_message_id FROM emails"):
        fresh.execute("UPDATE emails SET internet_message_id=? WHERE subject=?", tuple(table_row[::-1]))
    n = backup.import_data(fresh, data)
    assert n["decisions_applied"] == 1
    assert one(fresh, "SELECT COUNT(*) FROM review_items WHERE state='open'")[0] == 1


def test_merge_and_split_are_replayed_on_a_rebuilt_database(conn, tmp_path):
    run_emails(conn, [fx.HARLOW_CONFIRMATION, fx.REJECTION, fx.LINKEDIN_CONFIRMATION, fx.ASSESSMENT])
    appops.merge(conn, app_id(conn, "Northwind"), app_id(conn, "Calder"))
    rej = one(conn, "SELECT id FROM events WHERE category='rejection'")[0]
    appops.split(conn, app_id(conn, "Harlow"), [rej], [], "Harlow & Pine", "Other job")
    data = json.loads(json.dumps(backup.export_data(conn)))

    fresh = db.connect(tmp_path / "fresh.db")
    run_emails(fresh, [fx.HARLOW_CONFIRMATION, fx.REJECTION, fx.LINKEDIN_CONFIRMATION, fx.ASSESSMENT])
    # same emails in both databases need the same internet ids
    mapping = {r["subject"]: r["internet_message_id"] for r in conn.execute("SELECT subject, internet_message_id FROM emails")}
    for subj, mid in mapping.items():
        fresh.execute("UPDATE emails SET internet_message_id=? WHERE subject=?", (mid, subj))
    before = one(fresh, "SELECT COUNT(*) FROM applications")[0]
    n = backup.import_data(fresh, data)
    assert (n["merged"], n["split"]) == (1, 1)
    assert one(fresh, "SELECT COUNT(*) FROM applications")[0] == before            # one merge (-1) and one split (+1)
    assert one(fresh, "SELECT COUNT(*) FROM applications WHERE role_title='Other job'")[0] == 1


def test_import_rejects_files_that_are_not_backups(conn):
    with pytest.raises(ValueError):
        backup.import_data(conn, {"hello": "world"})
    with pytest.raises(ValueError):
        backup.import_data(conn, {"format": "job-tracker-backup", "version": 99})
