"""FastAPI app: server-rendered pages (Jinja2) with a little HTMX. Local use only."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import markdown as md
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import appops, auth, backup, calsync, db, gcal, lock, queries, review as review_ops, settings as settings_mod, sync as sync_mod
from ..classify import CATEGORIES
from ..config import Config, load_config
from ..engine import locked
from ..graph import GraphClient
from ..matching import norm_company, norm_role
from ..statuses import STATUSES
from ..timeutil import fmt_london, to_london, to_utc_iso

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

EDITABLE = ["company", "role_title", "location", "job_reference", "source", "status", "applied_at",
            "contact_name", "contact_email", "salary_range", "job_url"]
SHOWN = ["company", "role_title", "status", "applied_at", "job_url"]  # fields the detail page displays
LABELS = {
    "company": "Company", "role_title": "Role", "location": "Location", "job_reference": "Job reference",
    "source": "Source (job board / ATS)", "status": "Status", "applied_at": "Applied on",
    "contact_name": "Contact name", "contact_email": "Contact email", "salary_range": "Salary range",
    "job_url": "Job ad URL",
}


# --- template helpers ---------------------------------------------------------------------------------
def _slug(s: str) -> str:
    return (s or "").lower().replace(" ", "-")


templates.env.filters["london"] = lambda v, f="%a %d %b %Y, %H:%M": fmt_london(v, f)
templates.env.filters["ldate"] = lambda v: fmt_london(v, "%d %b %Y")
templates.env.filters["slug"] = _slug
templates.env.filters["markdown"] = lambda s: Markup(md.markdown(s or "", extensions=["nl2br", "sane_lists"]))
templates.env.filters["category_label"] = lambda c: (c or "").replace("_", " ").capitalize()
templates.env.filters["fromjson"] = lambda s: db.jloads(s, {}) or {}
templates.env.globals["STATUSES"] = STATUSES
templates.env.globals["CATEGORIES"] = CATEGORIES


# --- sync job state (one sync at a time) -------------------------------------------------------------
class SyncJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.lines: list[str] = []
        self.done = 0
        self.total = 0
        self.error: str | None = None
        self.finished = False


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    job = SyncJob()
    state = {"lock_pending": lock.foreign_lock(cfg.lock_path, cfg.machine_name), "lock_written": False}
    stop_beat = threading.Event()

    def beat() -> None:  # keep the lock fresh while the app runs
        while not stop_beat.wait(600):
            if state["lock_written"]:
                lock.write_lock(cfg.lock_path, cfg.machine_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not state["lock_pending"]:
            lock.write_lock(cfg.lock_path, cfg.machine_name)
            state["lock_written"] = True
        threading.Thread(target=beat, daemon=True).start()
        yield
        stop_beat.set()
        if state["lock_written"]:
            lock.release_lock(cfg.lock_path, cfg.machine_name)

    app = FastAPI(title="Job Application Tracker", lifespan=lifespan)
    app.state.sync_job = job   # exposed for tests
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # --- guards ---------------------------------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method == "POST":  # only accept form posts originating from this machine's pages
            origin = request.headers.get("origin")
            if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return HTMLResponse("Cross-site request refused.", status_code=403)
        path = request.url.path
        if state["lock_pending"] and not path.startswith(("/lock", "/static")):
            return RedirectResponse("/lock", status_code=303)
        return await call_next(request)

    def get_db():
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def ctx(conn: sqlite3.Connection, request: Request, active: str, **extra) -> dict:
        st = settings_mod.load(conn, cfg)
        return {
            "request": request, "active": active, "review_n": queries.review_count(conn), "settings": st,
            "flash": request.query_params.get("msg"), "flash_err": request.query_params.get("err"), **extra,
        }

    def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
        q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
        if q:
            url += ("&" if "?" in url else "?") + urlencode(q)
        return RedirectResponse(url, status_code=303)

    # --- lock warning ----------------------------------------------------------------------------
    @app.get("/lock", response_class=HTMLResponse)
    def lock_page(request: Request):
        if not state["lock_pending"]:
            return RedirectResponse("/", status_code=303)
        other = state["lock_pending"]
        return templates.TemplateResponse(request, "lock.html", {
            "request": request, "other": other, "when": fmt_london(other["time"]),
        })

    @app.post("/lock/continue")
    def lock_continue():
        lock.write_lock(cfg.lock_path, cfg.machine_name)
        state["lock_pending"] = None
        state["lock_written"] = True
        return RedirectResponse("/", status_code=303)

    # --- overview --------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        calsync.auto_complete(conn)
        c = ctx(conn, request, "overview")
        fu = queries.followup_list(conn, c["settings"].follow_up_days)
        c.update(
            counts=queries.status_counts(conn),
            total=conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            upcoming=queries.upcoming_interviews(conn, 14),
            followups=fu[:8], followup_total=len(fu),
            last_sync=db.kv_get(conn, "sync_state", "last_sync_at"),
            new_interviews=conn.execute("SELECT COUNT(*) FROM interviews WHERE status='New'").fetchone()[0],
            job=job, fresh=True,   # a normal page load never shows an old sync result
        )
        return templates.TemplateResponse(request, "overview.html", c)

    # --- applications ----------------------------------------------------------------------------
    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, q: str = "", status: str = "", sort: str = "activity", dir: str = "desc",
                     followup: int = 0, conn: sqlite3.Connection = Depends(get_db)):
        c = ctx(conn, request, "applications")
        rows = queries.list_applications(conn, c["settings"].follow_up_days, q, status, sort, dir, bool(followup))
        c.update(rows=rows, q=q, status=status, sort=sort, dir=dir, followup=followup)
        tpl = "_apps_table.html" if request.headers.get("HX-Request") else "applications.html"
        return templates.TemplateResponse(request, tpl, c)

    @app.post("/applications/new")
    def application_new(company: str = Form(...), role_title: str = Form(""), source: str = Form(""),
                        applied_on: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
        if not company.strip():
            return back("/applications", err="Company is required")
        now = db.utcnow()
        applied = to_utc_iso(applied_on + "T00:00:00") if applied_on else now
        lk = json.dumps(["company", "role_title", "source", "applied_at"])
        cur = conn.execute(
            """INSERT INTO applications (company, company_norm, role_title, role_norm, source, status, applied_at,
                   last_activity_at, locked_fields, created_manually, created_at, updated_at)
               VALUES (?,?,?,?,?,'Applied',?,?,?,1,?,?)""",
            (company.strip(), norm_company(company), role_title.strip(), norm_role(role_title),
             source.strip() or None, applied, applied, lk, now, now),
        )
        return RedirectResponse(f"/applications/{cur.lastrowid}", status_code=303)

    def _app_or_404(conn, app_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Application not found")
        return row

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application_detail(app_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        a = _app_or_404(conn, app_id)
        c = ctx(conn, request, "applications")
        events = conn.execute(
            """SELECT ev.*, em.not_filed_yet, em.subject, em.sender FROM events ev LEFT JOIN emails em ON em.id=ev.email_id
               WHERE ev.application_id=? ORDER BY ev.occurred_at DESC, ev.id DESC""", (app_id,)).fetchall()
        interviews = conn.execute("SELECT * FROM interviews WHERE application_id=? ORDER BY COALESCE(start_at, created_at)",
                                  (app_id,)).fetchall()
        applied_date = to_london(a["applied_at"]).strftime("%Y-%m-%d") if a["applied_at"] else ""
        c.update(a=a, events=events, interviews=interviews, locks=locked(a) & (set(SHOWN) | set(EDITABLE)), labels=LABELS,
                 other_apps=conn.execute("SELECT id, company, role_title FROM applications WHERE id<>? ORDER BY company COLLATE NOCASE", (app_id,)).fetchall(),
                 applied_date=applied_date, has_email=any(e["email_id"] for e in events),
                 followup=queries.needs_followup(a, c["settings"].follow_up_days),
                 days_since=queries.days_since(a["last_inbound_at"] or a["applied_at"] or a["created_at"]))
        return templates.TemplateResponse(request, "detail.html", c)

    @app.post("/applications/{app_id}/edit")
    async def application_edit(app_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        a = _app_or_404(conn, app_id)
        form = await request.form()
        updates: dict = {}
        for f in EDITABLE:
            if f not in form:
                continue
            raw = str(form[f]).strip()
            if f == "applied_at":
                cur = to_london(a["applied_at"]).strftime("%Y-%m-%d") if a["applied_at"] else ""
                if raw != cur:
                    updates[f] = to_utc_iso(raw + "T00:00:00") if raw else None
                continue
            if f == "status" and raw not in STATUSES:
                continue
            if f == "company" and not raw:
                return back(f"/applications/{app_id}", err="Company can't be empty")
            if raw != (a[f] or ""):
                updates[f] = raw or None
        if "notes" in form and str(form["notes"]) != a["notes"]:
            updates["notes"] = str(form["notes"])
        if not updates:
            return back(f"/applications/{app_id}", msg="No changes")
        extras = {}
        if "company" in updates:
            extras["company_norm"] = norm_company(updates["company"])
        if "role_title" in updates:
            extras["role_norm"] = norm_role(updates["role_title"])
            updates["role_title"] = updates["role_title"] or ""
        lk = locked(a) | {k for k in updates if k != "notes"}  # notes are always yours; no lock needed
        allv = {**updates, **extras, "locked_fields": json.dumps(sorted(lk)), "updated_at": db.utcnow()}
        conn.execute(f"UPDATE applications SET {', '.join(f'{k}=?' for k in allv)} WHERE id=?", (*allv.values(), app_id))
        return back(f"/applications/{app_id}", msg="Saved")

    @app.post("/applications/{app_id}/unlock/{field}")
    def application_unlock(app_id: int, field: str, conn: sqlite3.Connection = Depends(get_db)):
        a = _app_or_404(conn, app_id)
        lk = sorted(locked(a) - {field})
        conn.execute("UPDATE applications SET locked_fields=? WHERE id=?", (json.dumps(lk), app_id))
        return back(f"/applications/{app_id}", msg=f"{LABELS.get(field, field)} will be updated automatically again")

    @app.post("/applications/{app_id}/snooze")
    def application_snooze(app_id: int, days: int = Form(7), conn: sqlite3.Connection = Depends(get_db)):
        _app_or_404(conn, app_id)
        until = (datetime.now(timezone.utc) + timedelta(days=max(1, days))).isoformat(timespec="seconds")
        conn.execute("UPDATE applications SET snoozed_until=? WHERE id=?", (until, app_id))
        return back(f"/applications/{app_id}", msg=f"Follow-up snoozed for {days} days")

    @app.post("/applications/{app_id}/no-response")
    def application_no_response(app_id: int, conn: sqlite3.Connection = Depends(get_db)):
        a = _app_or_404(conn, app_id)
        lk = sorted(locked(a) | {"status"})
        conn.execute("UPDATE applications SET status='No response', locked_fields=?, updated_at=? WHERE id=?",
                     (json.dumps(lk), db.utcnow(), app_id))
        return back(f"/applications/{app_id}", msg="Marked as no response")

    @app.post("/applications/{app_id}/delete")
    def application_delete(app_id: int, conn: sqlite3.Connection = Depends(get_db)):
        _app_or_404(conn, app_id)
        conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        return back("/applications", msg="Application deleted")

    # --- interviews ------------------------------------------------------------------------------
    @app.get("/interviews", response_class=HTMLResponse)
    def interviews(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        calsync.auto_complete(conn)
        c = ctx(conn, request, "interviews")
        rows = conn.execute(
            """SELECT i.*, a.company, a.role_title FROM interviews i JOIN applications a ON a.id=i.application_id
               ORDER BY COALESCE(i.start_at, i.created_at)""").fetchall()
        groups = {s: [r for r in rows if r["status"] == s] for s in ("New", "Scheduled", "Completed", "Cancelled")}
        groups["Completed"].reverse()
        groups["Cancelled"].reverse()
        c.update(groups=groups)
        return templates.TemplateResponse(request, "interviews.html", c)

    @app.post("/interviews/{interview_id}/status")
    def interview_status(interview_id: int, status: str = Form(...), next: str = Form("/interviews"),
                         conn: sqlite3.Connection = Depends(get_db)):
        row = conn.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
        if not row or status not in ("New", "Scheduled", "Completed", "Cancelled"):
            return back("/interviews", err="Can't change that interview")
        if status == "Scheduled" and not row["start_at"]:
            return back(next, err="Set a time first (use New for items with no time)")
        lk = sorted(set(db.jloads(row["locked_fields"], [])) | {"status"})
        conn.execute("UPDATE interviews SET status=?, auto_completed=0, locked_fields=?, updated_at=? WHERE id=?",
                     (status, json.dumps(lk), db.utcnow(), interview_id))
        return back(next if next.startswith("/") else "/interviews", msg=f"Marked {status.lower()}")

    @app.post("/interviews/{interview_id}/google")
    def interview_google(interview_id: int, conn: sqlite3.Connection = Depends(get_db)):
        """Open Google Calendar's "new event" page, pre-filled. Nothing is created until you press Save there."""
        iv = _iv_or_404(conn, interview_id)
        app_row = conn.execute("SELECT * FROM applications WHERE id=?", (iv["application_id"],)).fetchone()
        try:
            url = gcal.google_url(iv, app_row)
        except gcal.NotEligible as e:
            return back("/interviews", err=str(e))
        conn.execute("UPDATE interviews SET google_added_at=? WHERE id=?", (db.utcnow(), interview_id))
        return RedirectResponse(url, status_code=303)

    @app.get("/interviews/{interview_id}/calendar.ics")
    def interview_ics(interview_id: int, conn: sqlite3.Connection = Depends(get_db)):
        iv = _iv_or_404(conn, interview_id)
        app_row = conn.execute("SELECT * FROM applications WHERE id=?", (iv["application_id"],)).fetchone()
        try:
            body = gcal.ics(iv, app_row)
        except gcal.NotEligible as e:
            return back("/interviews", err=str(e))
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="interview-{interview_id}.ics"'})

    @app.post("/interviews/{interview_id}/undo-complete")
    def interview_undo(interview_id: int, conn: sqlite3.Connection = Depends(get_db)):
        ok = calsync.undo_complete(conn, interview_id)
        return back("/interviews", msg="Moved back to Scheduled" if ok else "", err="" if ok else "Nothing to undo")

    # --- needs review ----------------------------------------------------------------------------
    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        c = ctx(conn, request, "review")
        items = []
        for r in conn.execute(
            """SELECT ri.*, em.subject, em.sender, em.received_at, em.web_link, em.category, em.not_filed_yet,
                      ce.subject AS ev_subject, ce.start_at AS ev_start, ce.end_at AS ev_end, ce.organizer AS ev_org,
                      ce.attendee_domains AS ev_domains, ce.web_link AS ev_link
               FROM review_items ri LEFT JOIN emails em ON em.id=ri.email_id
                    LEFT JOIN calendar_events ce ON ce.graph_id=ri.calendar_event_id
               WHERE ri.state='open' ORDER BY COALESCE(em.received_at, ce.start_at) DESC"""):
            sug = review_ops.suggestion_of(r)
            ids = sug.get("candidate_application_ids", [])
            cands = [dict(x) for x in conn.execute(
                f"SELECT id, company, role_title, status FROM applications WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
            items.append({"r": r, "sug": sug, "cands": cands, "domains": db.jloads(r["ev_domains"], [])})
        apps = conn.execute("SELECT id, company, role_title FROM applications ORDER BY company COLLATE NOCASE").fetchall()
        c.update(items=items, apps=apps)
        return templates.TemplateResponse(request, "review.html", c)

    def _link_id(link_to: str) -> int | None:
        import re as _re

        m = _re.search(r"#(\d+)\s*$", link_to or "")
        return int(m.group(1)) if m else None

    @app.post("/review/{item_id}/calendar-accept")
    def review_calendar_accept(item_id: int, link_to: str = Form(""), company: str = Form(""), role_title: str = Form(""),
                               conn: sqlite3.Connection = Depends(get_db)):
        app_id = _link_id(link_to) if link_to.strip() else None
        if link_to.strip() and app_id is None:
            return back("/review", err="Choose an application from the list")
        try:
            aid = calsync.accept_event(conn, item_id, app_id, company, role_title)
        except calsync.CalendarReviewError as e:
            return back("/review", err=str(e))
        return back("/review", msg=f"Interview added to application #{aid}")

    @app.post("/review/{item_id}/calendar-dismiss")
    def review_calendar_dismiss(item_id: int, conn: sqlite3.Connection = Depends(get_db)):
        try:
            calsync.dismiss_event(conn, item_id)
        except calsync.CalendarReviewError as e:
            return back("/review", err=str(e))
        return back("/review", msg="Calendar event dismissed (it won't be suggested again)")

    def _me(conn) -> str:
        return db.kv_get(conn, "sync_state", "me_address", "") or ""

    @app.post("/review/{item_id}/accept")
    async def review_accept(item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        form = await request.form()
        overrides = {k: str(form[k]).strip() for k in ("company", "role_title", "category") if k in form}
        link_to = str(form.get("link_to", "")).strip()
        app_id = None
        if link_to:
            import re as _re

            m = _re.search(r"#(\d+)\s*$", link_to)
            if not m:
                return back("/review", err="Choose an application from the list")
            app_id = int(m.group(1))
        try:
            aid = review_ops.accept(conn, item_id, overrides, app_id, _me(conn))
        except review_ops.ReviewError as e:
            return back("/review", err=str(e))
        return back("/review", msg=f"Added to application #{aid}")

    @app.post("/review/{item_id}/dismiss")
    def review_dismiss(item_id: int, conn: sqlite3.Connection = Depends(get_db)):
        try:
            review_ops.dismiss(conn, item_id)
        except review_ops.ReviewError as e:
            return back("/review", err=str(e))
        return back("/review", msg="Dismissed as not job-related")

    # --- sync ------------------------------------------------------------------------------------
    def _run_sync() -> None:
        conn = None
        try:
            conn = db.connect(cfg.db_path)
            g = GraphClient(lambda: auth.get_token(cfg, interactive=False))

            def out(line: str) -> None:
                with job.lock:
                    job.lines.append(line)

            def progress(done: int, total: int) -> None:
                job.done, job.total = done, total

            def confirm(prompt: str) -> bool:  # first big sync must be confirmed in the terminal
                out("This sync needs a cost confirmation. Run  python -m tracker sync  in a terminal for the first full scan.")
                return False

            sync_mod.run(cfg, g, conn, out=out, progress=progress, confirm=confirm)
        except Exception as e:  # noqa: BLE001
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.running = False
            job.finished = True
            if conn:
                conn.close()

    @app.post("/sync", response_class=HTMLResponse)
    def sync_start(request: Request):
        if not job.running:
            job.reset()
            job.running = True
            threading.Thread(target=_run_sync, daemon=True).start()
        return templates.TemplateResponse(request, "_sync.html", {"request": request, "job": job, "fresh": False})

    @app.get("/sync/status", response_class=HTMLResponse)
    def sync_status(request: Request):
        return templates.TemplateResponse(request, "_sync.html", {"request": request, "job": job, "fresh": False})

    # --- merge / split ---------------------------------------------------------------------------
    def _link_id2(text: str) -> int | None:
        import re as _re

        m = _re.search(r"#(\d+)\s*$", text or "")
        return int(m.group(1)) if m else None

    @app.post("/applications/{app_id}/merge")
    def application_merge(app_id: int, target: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
        _app_or_404(conn, app_id)
        tid = _link_id2(target)
        if tid is None:
            return back(f"/applications/{app_id}", err="Choose the application to merge into from the list")
        try:
            appops.merge(conn, app_id, tid)
        except appops.OpError as e:
            return back(f"/applications/{app_id}", err=str(e))
        return back(f"/applications/{tid}", msg="Merged: everything now lives on this application")

    @app.get("/applications/{app_id}/split", response_class=HTMLResponse)
    def application_split_page(app_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        a = _app_or_404(conn, app_id)
        events = conn.execute("SELECT * FROM events WHERE application_id=? ORDER BY occurred_at DESC, id DESC", (app_id,)).fetchall()
        ivs = conn.execute("SELECT * FROM interviews WHERE application_id=? ORDER BY COALESCE(start_at, created_at)", (app_id,)).fetchall()
        c = ctx(conn, request, "applications")
        c.update(a=a, events=events, interviews=ivs)
        return templates.TemplateResponse(request, "split.html", c)

    @app.post("/applications/{app_id}/split")
    async def application_split(app_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        _app_or_404(conn, app_id)
        form = await request.form()
        ev = [int(x) for x in form.getlist("event") if str(x).isdigit()]
        iv = [int(x) for x in form.getlist("interview") if str(x).isdigit()]
        try:
            new_id = appops.split(conn, app_id, ev, iv, str(form.get("company", "")), str(form.get("role_title", "")))
        except appops.OpError as e:
            return back(f"/applications/{app_id}/split", err=str(e))
        return back(f"/applications/{new_id}", msg="Split into a new application")

    # --- interviews by hand ----------------------------------------------------------------------
    def _interview_ctx(conn, request, active_app_id=None, iv=None):
        c = ctx(conn, request, "interviews")
        apps = conn.execute("SELECT id, company, role_title FROM applications ORDER BY company COLLATE NOCASE").fetchall()
        sel = None
        if iv is not None:
            sel = conn.execute("SELECT id, company, role_title FROM applications WHERE id=?", (iv["application_id"],)).fetchone()
        elif active_app_id:
            sel = conn.execute("SELECT id, company, role_title FROM applications WHERE id=?", (active_app_id,)).fetchone()
        fmt = lambda v: to_london(v).strftime("%Y-%m-%dT%H:%M") if v else ""
        c.update(apps=apps, sel=sel, iv=iv, start_val=fmt(iv["start_at"]) if iv else "", end_val=fmt(iv["end_at"]) if iv else "",
                 kinds=appops.KINDS, states=appops.STATES, formats=appops.FORMATS)
        return c

    @app.get("/interviews/new", response_class=HTMLResponse)
    def interview_new_page(request: Request, app: int = 0, conn: sqlite3.Connection = Depends(get_db)):
        return templates.TemplateResponse(request, "interview_form.html", _interview_ctx(conn, request, app or None))

    @app.post("/interviews/new")
    def interview_new(application: str = Form(""), kind: str = Form("interview"), status: str = Form("New"),
                      start: str = Form(""), end: str = Form(""), stage: str = Form(""), fmt: str = Form(""),
                      conn: sqlite3.Connection = Depends(get_db)):
        app_id = _link_id2(application)
        if app_id is None:
            return back("/interviews/new", err="Choose an application from the list")
        try:
            appops.create_interview(conn, app_id, kind, status, start, end, stage, fmt)
        except appops.OpError as e:
            return back(f"/interviews/new?app={app_id}", err=str(e))
        return back(f"/applications/{app_id}", msg="Interview added")

    def _iv_or_404(conn, interview_id: int):
        row = conn.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Interview not found")
        return row

    @app.get("/interviews/{interview_id}/edit", response_class=HTMLResponse)
    def interview_edit_page(interview_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        iv = _iv_or_404(conn, interview_id)
        return templates.TemplateResponse(request, "interview_form.html", _interview_ctx(conn, request, iv=iv))

    @app.post("/interviews/{interview_id}/edit")
    def interview_edit(interview_id: int, kind: str = Form("interview"), status: str = Form("New"), start: str = Form(""),
                       end: str = Form(""), stage: str = Form(""), fmt: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
        _iv_or_404(conn, interview_id)
        try:
            app_id = appops.update_interview(conn, interview_id, kind, status, start, end, stage, fmt)
        except appops.OpError as e:
            return back(f"/interviews/{interview_id}/edit", err=str(e))
        return back(f"/applications/{app_id}", msg="Interview saved")

    @app.post("/interviews/{interview_id}/delete")
    def interview_delete(interview_id: int, conn: sqlite3.Connection = Depends(get_db)):
        app_id = appops.delete_interview(conn, interview_id)
        return back(f"/applications/{app_id}" if app_id else "/interviews", msg="Interview deleted")

    # --- settings --------------------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        c = ctx(conn, request, "settings")
        c.update(
            db_path=str(cfg.db_path), machine=cfg.machine_name, last_sync=db.kv_get(conn, "sync_state", "last_sync_at"),
            schema=conn.execute("PRAGMA user_version").fetchone()[0], token_path=str(cfg.token_cache_path),
            models=["claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"],
        )
        return templates.TemplateResponse(request, "settings.html", c)

    def _lines(text: str) -> list[str]:
        out, seen = [], set()
        for ln in (text or "").replace(",", "\n").splitlines():
            ln = ln.strip().lower()
            if ln and ln not in seen:
                seen.add(ln)
                out.append(ln)
        return out

    @app.post("/settings")
    def settings_save(follow_up_days: int = Form(...), confidence_threshold: float = Form(...), claude_model: str = Form(...),
                      applications_folder: str = Form(...), inbox_check: str = Form(""), calendar_check: str = Form(""),
                      sender_domains: str = Form(""), keywords: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
        if not (1 <= follow_up_days <= 365):
            return back("/settings", err="Follow-up days must be between 1 and 365")
        if not (0 <= confidence_threshold <= 1):
            return back("/settings", err="Confidence threshold must be between 0 and 1")
        if not claude_model.strip() or not applications_folder.strip():
            return back("/settings", err="Model and folder name can't be empty")
        with db.tx(conn):
            settings_mod.save(conn, "follow_up_days", follow_up_days)
            settings_mod.save(conn, "confidence_threshold", confidence_threshold)
            settings_mod.save(conn, "claude_model", claude_model.strip())
            settings_mod.save(conn, "applications_folder", applications_folder.strip())
            settings_mod.save(conn, "inbox_check", bool(inbox_check))
            settings_mod.save(conn, "calendar_check", bool(calendar_check))
            settings_mod.save(conn, "sender_domains", _lines(sender_domains))
            settings_mod.save(conn, "keywords", [k.strip().lower() for k in (keywords or "").splitlines() if k.strip()])
        return back("/settings", msg="Settings saved")

    @app.get("/settings/export")
    def settings_export(conn: sqlite3.Connection = Depends(get_db)):
        data = backup.export_data(conn)
        name = f"job-tracker-backup-{datetime.now().strftime('%Y-%m-%d')}.json"
        return Response(json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/settings/import")
    async def settings_import(file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_db)):
        try:
            data = json.loads((await file.read()).decode("utf-8"))
            n = backup.import_data(conn, data, _me(conn))
        except (ValueError, KeyError, TypeError) as e:
            return back("/settings", err=f"Import failed: {e}")
        return back("/settings", msg=(
            f"Imported: {n['applications_updated']} applications updated, {n['applications_created']} created, "
            f"{n['interviews']} interviews, {n['decisions_applied']} review decisions, {n['calendar_dismissed']} calendar dismissals, "
            f"{n['merged']} merges and {n['split']} splits replayed"
            + (f" ({n['applications_missing']} backed-up applications weren't found; run a sync first)" if n['applications_missing'] else "")))

    return app
