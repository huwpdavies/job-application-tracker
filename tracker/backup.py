"""Export/import of everything that is *yours* (not re-derivable from email): notes, manual edits, review decisions.

Typical restore: create a fresh database, run a sync (rebuilds applications from email, free thanks to the
classification cache), then import this file to put your notes, edits and review decisions back.
"""
from __future__ import annotations

import json
import sqlite3

from collections import Counter

from . import appops, db, review as review_ops
from .classify import Classification
from .engine import locked
from .matching import norm_company, norm_role

FORMAT_VERSION = 1
APP_FIELDS = ["company", "role_title", "location", "job_reference", "source", "status", "applied_at",
              "contact_name", "contact_email", "salary_range", "job_url"]


def _key(company: str, role: str) -> dict:
    return {"company_norm": norm_company(company), "role_norm": norm_role(role)}


def _app_key(conn: sqlite3.Connection, app: sqlite3.Row) -> dict:
    """Identify an application by its emails (stable even if you rename it), with the names as a fallback."""
    ids = [r[0] for r in conn.execute(
        "SELECT internet_message_id FROM emails WHERE application_id=? AND internet_message_id IS NOT NULL ORDER BY received_at LIMIT 5",
        (app["id"],))]
    return {**_key(app["company"], app["role_title"]), "email_ids": ids}


def _find_app(conn: sqlite3.Connection, key: dict) -> sqlite3.Row | None:
    ids = key.get("email_ids") or []
    if ids:
        marks = ",".join("?" * len(ids))
        row = conn.execute(
            f"""SELECT a.* FROM applications a JOIN emails e ON e.application_id=a.id
                WHERE e.internet_message_id IN ({marks}) GROUP BY a.id ORDER BY COUNT(*) DESC, a.id LIMIT 1""", ids).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM applications WHERE company_norm=? AND role_norm=? ORDER BY id LIMIT 1",
                        (key["company_norm"], key["role_norm"])).fetchone()


def _app_key_by_id(conn: sqlite3.Connection, app_id: int | None) -> dict | None:
    r = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone() if app_id else None
    return _app_key(conn, r) | {"company": r["company"], "role_title": r["role_title"]} if r else None


# ---------------------------------------------------------------------------------------------------
def export_data(conn: sqlite3.Connection) -> dict:
    apps = []
    for a in conn.execute(
        """SELECT * FROM applications WHERE locked_fields<>'[]' OR notes<>'' OR created_manually=1 OR snoozed_until IS NOT NULL
           OR COALESCE(contact_name,'')<>'' OR COALESCE(contact_email,'')<>'' OR COALESCE(salary_range,'')<>''
           OR COALESCE(job_url,'')<>'' """):
        apps.append({
            "key": _app_key(conn, a),
            "created_manually": bool(a["created_manually"]),
            "locked_fields": sorted(locked(a)),
            "notes": a["notes"],
            "snoozed_until": a["snoozed_until"],
            "fields": {f: a[f] for f in APP_FIELDS},
        })
    interviews = []
    for i in conn.execute("SELECT i.*, a.company, a.role_title FROM interviews i JOIN applications a ON a.id=i.application_id "
                          "WHERE i.created_manually=1 OR i.locked_fields<>'[]'"):
        interviews.append({
            "app_key": _app_key(conn, conn.execute("SELECT * FROM applications WHERE id=?", (i["application_id"],)).fetchone()),
            **{k: i[k] for k in ("kind", "status", "start_at", "end_at", "format", "stage")},
            "locked_fields": db.jloads(i["locked_fields"], []), "created_manually": bool(i["created_manually"]),
        })
    decisions = []
    for r in conn.execute(
        """SELECT ri.state, e.internet_message_id, e.classification, e.application_id, e.subject
           FROM review_items ri JOIN emails e ON e.id=ri.email_id
           WHERE ri.kind='email' AND ri.state<>'open' AND e.internet_message_id IS NOT NULL"""):
        decisions.append({
            "internet_message_id": r["internet_message_id"], "decision": r["state"], "subject": r["subject"],
            "classification": db.jloads(r["classification"], {}),
            "application": _app_key_by_id(conn, r["application_id"]),
        })
    cal = [{"graph_id": r["graph_id"], "subject": r["subject"], "start_at": r["start_at"]}
           for r in conn.execute("SELECT * FROM calendar_events WHERE state='dismissed'")]
    groups = []
    for a in conn.execute("SELECT * FROM applications"):
        ids = [r[0] for r in conn.execute(
            "SELECT internet_message_id FROM emails WHERE application_id=? AND internet_message_id IS NOT NULL", (a["id"],))]
        if ids:
            groups.append({"company": a["company"], "role_title": a["role_title"], "emails": ids})
    return {
        "format": "job-tracker-backup", "version": FORMAT_VERSION, "exported_at": db.utcnow(),
        "settings": {r["key"]: db.jloads(r["value"]) for r in conn.execute("SELECT * FROM settings")},
        "structure": groups,
        "applications": apps, "interviews": interviews, "review_decisions": decisions, "dismissed_calendar_events": cal,
    }


# ---------------------------------------------------------------------------------------------------
def import_data(conn: sqlite3.Connection, data: dict, me_address: str = "") -> dict:
    if data.get("format") != "job-tracker-backup" or data.get("version", 0) > FORMAT_VERSION:
        raise ValueError("This isn't a Job Tracker backup file (or it's from a newer version).")
    n = {"merged": 0, "split": 0, "settings": 0, "applications_updated": 0, "applications_created": 0, "applications_missing": 0,
         "interviews": 0, "decisions_applied": 0, "decisions_skipped": 0, "calendar_dismissed": 0}
    now = db.utcnow()
    with db.tx(conn):
        _restore_structure(conn, data.get("structure") or [], n)

        for k, v in (data.get("settings") or {}).items():
            db.kv_set(conn, "settings", k, json.dumps(v))
            n["settings"] += 1

        for a in data.get("applications", []):
            f = a.get("fields", {})
            row = _find_app(conn, a["key"])
            if row is None:
                if not a.get("created_manually"):
                    n["applications_missing"] += 1
                    continue
                conn.execute(
                    """INSERT INTO applications (company, company_norm, role_title, role_norm, location, job_reference, source,
                           status, applied_at, contact_name, contact_email, salary_range, job_url, notes, snoozed_until,
                           locked_fields, created_manually, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (f.get("company"), norm_company(f.get("company")), f.get("role_title") or "", norm_role(f.get("role_title")),
                     f.get("location"), f.get("job_reference"), f.get("source"), f.get("status") or "Applied", f.get("applied_at"),
                     f.get("contact_name"), f.get("contact_email"), f.get("salary_range"), f.get("job_url"), a.get("notes", ""),
                     a.get("snoozed_until"), json.dumps(a.get("locked_fields", [])), now, now))
                n["applications_created"] += 1
                continue
            lk = set(a.get("locked_fields", [])) | locked(row)
            vals = {fld: f.get(fld) for fld in a.get("locked_fields", []) if fld in APP_FIELDS}  # hand-edited values win
            for fld in ("contact_name", "contact_email", "salary_range", "job_url"):  # structured fields: fill if empty
                if f.get(fld) and not row[fld]:
                    vals[fld] = f[fld]
            if a.get("notes") and a["notes"] != row["notes"]:
                vals["notes"] = a["notes"]
            if a.get("snoozed_until"):
                vals["snoozed_until"] = a["snoozed_until"]
            if "company" in vals and vals["company"]:
                vals["company_norm"] = norm_company(vals["company"])
            if "role_title" in vals:
                vals["role_title"] = vals["role_title"] or ""
                vals["role_norm"] = norm_role(vals["role_title"])
            vals["locked_fields"] = json.dumps(sorted(lk))
            conn.execute(f"UPDATE applications SET {', '.join(f'{k}=?' for k in vals)}, updated_at=? WHERE id=?",
                         (*vals.values(), now, row["id"]))
            n["applications_updated"] += 1

        for i in data.get("interviews", []):
            app = _find_app(conn, i["app_key"])
            if not app:
                continue
            exists = conn.execute("SELECT 1 FROM interviews WHERE application_id=? AND kind=? AND COALESCE(start_at,'')=COALESCE(?,'')",
                                  (app["id"], i.get("kind", "interview"), i.get("start_at"))).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO interviews (application_id, kind, status, start_at, end_at, format, stage, locked_fields,
                       created_manually, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (app["id"], i.get("kind", "interview"), i["status"], i.get("start_at"), i.get("end_at"), i.get("format"),
                 i.get("stage"), json.dumps(i.get("locked_fields", [])), int(i.get("created_manually", False)), now, now))
            n["interviews"] += 1

        for d in data.get("review_decisions", []):
            item = conn.execute(
                """SELECT ri.id FROM review_items ri JOIN emails e ON e.id=ri.email_id
                   WHERE e.internet_message_id=? AND ri.kind='email' AND ri.state='open'""", (d["internet_message_id"],)).fetchone()
            if not item:
                n["decisions_skipped"] += 1  # not in this database yet, or already handled
                continue
            try:
                if d["decision"] == "dismissed":
                    review_ops.dismiss(conn, item["id"], own_tx=False)
                else:
                    cls = d.get("classification") or {}
                    app = _find_app(conn, d["application"]) if d.get("application") else None
                    overrides = {k: cls.get(k) for k in ("company", "role_title", "category") if cls.get(k)}
                    review_ops.accept(conn, item["id"], overrides, app["id"] if app else None, me_address, own_tx=False)
                n["decisions_applied"] += 1
            except review_ops.ReviewError:
                n["decisions_skipped"] += 1

        for c in data.get("dismissed_calendar_events", []):
            conn.execute(
                """INSERT INTO calendar_events (graph_id, subject, start_at, state) VALUES (?,?,?, 'dismissed')
                   ON CONFLICT(graph_id) DO UPDATE SET state='dismissed'""", (c["graph_id"], c.get("subject"), c.get("start_at")))
            conn.execute("UPDATE review_items SET state='dismissed' WHERE kind='calendar' AND calendar_event_id=? AND state='open'", (c["graph_id"],))
            n["calendar_dismissed"] += 1
    return n


def _restore_structure(conn: sqlite3.Connection, groups: list[dict], n: dict) -> None:
    """Replay merges and splits: make the emails in each backed-up group belong to one application, and apart from
    the other groups. Emails the backup doesn't know about (new mail) are left where they are."""
    owner = {r["internet_message_id"]: r["application_id"] for r in conn.execute(
        "SELECT internet_message_id, application_id FROM emails WHERE application_id IS NOT NULL AND internet_message_id IS NOT NULL")}
    group_of = {i: gi for gi, g in enumerate(groups) for i in g["emails"]}
    for gi, g in enumerate(groups):
        present = [i for i in g["emails"] if i in owner]
        if not present:
            continue
        counts = Counter(owner[i] for i in present)
        main = counts.most_common(1)[0][0]
        for other in list(counts):  # merged in the backup: bring the pieces together
            if other != main:
                appops.merge(conn, other, main, own_tx=False)
                for k, v in owner.items():
                    if v == other:
                        owner[k] = main
                n["merged"] += 1
        # split in the backup: this group shares an application with emails that belong to a different group
        foreign = [k for k, v in owner.items() if v == main and k not in set(g["emails"]) and group_of.get(k, gi) != gi]
        if foreign:
            ev = [r["id"] for r in conn.execute(
                f"SELECT ev.id FROM events ev JOIN emails e ON e.id=ev.email_id WHERE ev.application_id=? "
                f"AND e.internet_message_id IN ({','.join('?' * len(present))})", (main, *present))]
            try:
                new_id = appops.split(conn, main, ev, [], g["company"], g["role_title"] or "", own_tx=False)
            except appops.OpError:
                continue
            for i in present:
                owner[i] = new_id
            n["split"] += 1
