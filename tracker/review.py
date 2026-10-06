"""Needs-review actions: accept, edit-then-accept, link to another application, dismiss."""
from __future__ import annotations

import sqlite3
from contextlib import nullcontext

from . import db
from .classify import CATEGORIES, Classification
from .engine import Summary, commit_to_application
from .matching import find_match


class ReviewError(ValueError):
    pass


def _load(conn: sqlite3.Connection, item_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    item = conn.execute("SELECT * FROM review_items WHERE id=? AND state='open'", (item_id,)).fetchone()
    if not item:
        raise ReviewError("That review item has already been handled.")
    if item["kind"] != "email":
        raise ReviewError("Calendar review items are handled elsewhere.")
    email = conn.execute("SELECT * FROM emails WHERE id=?", (item["email_id"],)).fetchone()
    return item, email


def suggestion_of(item: sqlite3.Row) -> dict:
    return db.jloads(item["suggestion"], {}) or {}


def _classification(item: sqlite3.Row, overrides: dict | None) -> Classification:
    data = {k: v for k, v in suggestion_of(item).items() if k in Classification.model_fields}
    for k, v in (overrides or {}).items():
        if v is not None and v != "":
            data[k] = v
    data.setdefault("is_job_related", True)
    data["is_job_related"] = True  # accepting means "this is job related"
    data["confidence"] = 1.0  # a human confirmed it
    if data.get("category") not in CATEGORIES:
        raise ReviewError("Pick a valid category.")
    cls = Classification.model_validate(data)
    return cls


def accept(conn: sqlite3.Connection, item_id: int, overrides: dict | None = None, app_id: int | None = None,
           me_address: str = "", own_tx: bool = True) -> int:
    """Apply a review item to an application. app_id=None: use the best match, else create a new application."""
    item, email = _load(conn, item_id)
    cls = _classification(item, overrides)
    if cls.category == "other":
        raise ReviewError("Category 'other' can't be added to an application. Use Dismiss instead.")
    if not cls.company:
        raise ReviewError("A company name is needed (use Edit).")
    linked = app_id is not None
    if app_id is None:
        app_id = find_match(conn, cls.company, cls.role_title, cls.job_reference).app_id
    elif not conn.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone():
        raise ReviewError("That application no longer exists.")
    with (db.tx(conn) if own_tx else nullcontext()):
        conn.execute("UPDATE emails SET category=?, classification=?, confidence=1.0 WHERE id=?",
                     (cls.category, cls.model_dump_json(), email["id"]))
        app_id = commit_to_application(
            conn, email["id"], cls, email["received_at"], email["web_link"] or "", app_id,
            inbound=bool(email["sender"]) and email["sender"].lower() != me_address.lower(), summary=Summary(),
        )
        conn.execute("UPDATE review_items SET state=? WHERE id=?", ("linked" if linked else "accepted", item_id))
    return app_id


def dismiss(conn: sqlite3.Connection, item_id: int, own_tx: bool = True) -> None:
    item, email = _load(conn, item_id)
    with (db.tx(conn) if own_tx else nullcontext()):
        conn.execute("UPDATE emails SET state='dismissed' WHERE id=?", (email["id"],))
        conn.execute("UPDATE review_items SET state='dismissed' WHERE id=?", (item_id,))
