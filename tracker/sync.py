"""Sync: fetch new mail, classify with Claude, apply to the database. Also powers `--dry-run`."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import calsync, db, settings as settings_mod
from .classify import SYSTEM, TOOL, ClassCache, Classification, Classifier, Usage, cost_usd, estimate_tokens
from .config import Config
from . import review as review_ops
from .engine import Summary, find_existing, handle_email, note_moved
from .graph import GraphClient, GraphError
from .matching import find_match
from .mail import SELECT, Message, default_since, folder_tree, message_from_graph
from .prefilter import domain_matches, keyword_hit
from .textclean import clean_body
from .timeutil import to_utc_iso

OUTPUT_TOKENS_PER_EMAIL = 160
PROMPT_OVERHEAD = estimate_tokens(SYSTEM) + estimate_tokens(str(TOOL)) + 60
CONFIRM_ABOVE_USD = 2.0  # later syncs only ask if the estimate is unusually large


@dataclass
class Work:
    msg: Message
    folder: str  # 'applications' | 'inbox'
    text: str = ""
    cls: Classification | None = None
    error: str | None = None
    cached: bool = False


def estimate(model: str, works: list[Work]) -> tuple[int, int, float]:
    todo = [w for w in works if not w.cached]
    tin = sum(PROMPT_OVERHEAD + estimate_tokens(w.text) for w in todo)
    tout = OUTPUT_TOKENS_PER_EMAIL * len(todo)
    return tin, tout, cost_usd(model, Usage(tin, tout))


def _since_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_folder_changes(conn, g: GraphClient, folder_id: str, since: datetime) -> tuple[list[Message], str | None]:
    saved = db.kv_get(conn, "sync_state", f"delta:{folder_id}")
    raw, link = g.delta_messages(folder_id, SELECT, saved, None if saved else _since_iso(since))
    msgs = [message_from_graph(m) for m in raw]
    if not saved:  # first run for this folder: enforce the window locally too
        cutoff = to_utc_iso(since)
        msgs = [m for m in msgs if m.received_at >= cutoff]
    return msgs, link


def _inbox_candidates(conn, g: GraphClient, st, since: datetime, out) -> list[Message]:
    """Inbox emails from senders already linked to a tracked application (the 'Not filed yet' check)."""
    rows = conn.execute(
        "SELECT DISTINCT lower(sender) AS s, sender_domain AS d FROM emails WHERE application_id IS NOT NULL AND folder='applications'"
    ).fetchall()
    addresses = {r["s"] for r in rows if r["s"]}
    domains = {r["d"] for r in rows if r["d"] and not domain_matches(r["d"], st.sender_domains)}  # not shared job-board/ATS domains
    if not addresses and not domains:
        return []
    params = {
        "$filter": f"receivedDateTime ge {_since_iso(since)}",
        "$orderby": "receivedDateTime desc",
        "$select": "id,internetMessageId,subject,from,receivedDateTime,webLink,parentFolderId,bodyPreview",
        "$top": 100,
    }
    found: list[Message] = []
    for m in g.paged("/me/mailFolders/inbox/messages", params):
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        domain = sender.rsplit("@", 1)[-1]
        text = f"{m.get('subject') or ''} {m.get('bodyPreview') or ''}"
        ok = domain in domains or (sender in addresses and keyword_hit(text, st.keywords))
        if not ok or find_existing(conn, m.get("internetMessageId"), m["id"]):
            continue
        full = g.get(f"/me/messages/{m['id']}", {"$select": SELECT})
        found.append(message_from_graph(full))
    out(f"  Inbox: {len(found)} email(s) from known application senders")
    return found


def _retry_unmatched(conn, summary: Summary, me_addr: str) -> None:
    """A later email may have created the application an earlier, unmatched one belongs to (e.g. a recruiter
    introduction that arrives before the interview invite). Re-check open 'no matching application' items."""
    rows = conn.execute(
        "SELECT * FROM review_items WHERE state='open' AND kind='email' AND reason LIKE 'No matching application%' "
        "ORDER BY id"
    ).fetchall()
    for item in rows:
        sug = review_ops.suggestion_of(item)
        if not sug.get("company"):
            continue
        m = find_match(conn, sug["company"], sug.get("role_title"), sug.get("job_reference"))
        if m.app_id is None:
            continue
        try:
            review_ops.accept(conn, item["id"], None, m.app_id, me_addr, own_tx=False)
        except review_ops.ReviewError:
            continue
        summary.needs_review -= 1
        summary.log(f"RELINKED review item {item['id']} to application #{m.app_id} ({sug['company']})")


def run(
    cfg: Config,
    g: GraphClient,
    conn,
    *,
    dry_run: bool = False,
    folder_name: str | None = None,
    months: int = 6,
    limit: int | None = None,
    confirm: Callable[[str], bool] | None = None,
    out: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
    verbose: bool = False,
) -> Summary | None:
    """Returns the Summary, or None if cancelled / failed before any work."""
    st = settings_mod.load(conn, cfg)
    folder_name = folder_name or st.applications_folder
    started = db.utcnow()

    # 1. Locate the folder (id is remembered; looked up again if the name changed).
    fid = db.kv_get(conn, "sync_state", "folder_id")
    if not fid or db.kv_get(conn, "sync_state", "folder_name") != folder_name:
        f = g.find_folder(folder_name)
        if not f:
            out(f"Could not find a mail folder called '{folder_name}'. Change it in Settings.")
            return None
        fid = f["id"]
        # Remembered after a successful (non-dry) sync below.
    tree = folder_tree(g, fid, folder_name)
    me = g.me()
    me_addr = (me.get("mail") or me.get("userPrincipalName") or "").lower()

    first_run = db.kv_get(conn, "sync_state", "last_sync_at") is None
    since = default_since(months)
    out(f"Folder '{folder_name}': {len(tree)} folder(s) including subfolders" + (" (first run)" if first_run else ""))

    # 2. Collect new mail
    works: list[Work] = []
    new_links: dict[str, str] = {}
    seen: set[str] = set()
    summary = Summary()
    moved_known = 0
    retry_delete: list[int] = []
    for folder_id, path in tree:
        msgs, link = _fetch_folder_changes(conn, g, folder_id, since)
        if link:
            new_links[folder_id] = link
        n = 0
        for m in msgs:
            key = m.internet_message_id or m.graph_id
            if key in seen:
                continue
            seen.add(key)
            existing = find_existing(conn, m.internet_message_id, m.graph_id)
            if existing:
                if existing["state"] != "error":
                    if note_moved(conn, existing, m, "applications") if not dry_run else False:
                        moved_known += 1
                    summary.skipped_known += 1
                    continue
                retry_delete.append(existing["id"])  # retry a previously failed email
            works.append(Work(m, "applications"))
            n += 1
        out(f"  {path}: {n} new email(s)")

    # Emails that failed to classify earlier are not returned by delta again, so fetch them by id and retry.
    for row in conn.execute("SELECT * FROM emails WHERE state='error' ORDER BY received_at").fetchall():
        key = row["internet_message_id"] or row["graph_id"]
        if key in seen:
            continue
        try:
            m = message_from_graph(g.get(f"/me/messages/{row['graph_id']}", {"$select": SELECT}))
        except (GraphError, KeyError):
            continue  # moved or deleted since; leave it recorded as failed
        seen.add(key)
        retry_delete.append(row["id"])
        works.append(Work(m, row["folder"] or "applications"))

    if st.inbox_check and not first_run:
        cursor = db.kv_get(conn, "sync_state", "inbox_cursor") or db.kv_get(conn, "sync_state", "last_sync_at")
        since_inbox = datetime.fromisoformat(cursor) - timedelta(hours=1) if cursor else since
        for m in _inbox_candidates(conn, g, st, since_inbox, out):
            if (m.internet_message_id or m.graph_id) not in seen:
                works.append(Work(m, "inbox"))

    works.sort(key=lambda w: w.msg.received_at)
    if limit and len(works) > limit:
        works = works[-limit:]  # the most recent N
        new_links = {}  # partial fetch: don't advance delta tokens
        out(f"Limiting to the {len(works)} most recent emails (--limit)")

    # 3. Cost estimate and confirmation
    cache = ClassCache(cfg.token_cache_path.with_name("classification_cache.jsonl"))
    for w in works:
        w.text = clean_body(w.msg.body, w.msg.body_type)
        if w.msg.internet_message_id:
            w.cls = cache.get(cache.key(w.msg.internet_message_id, st.claude_model))
            w.cached = w.cls is not None
    todo = [w for w in works if not w.cached]
    if works:
        tin, tout, est = estimate(st.claude_model, works)
        out(
            f"\n{len(works)} email(s) to process ({len(works) - len(todo)} already classified in the local cache).\n"
            + (f"Estimated Claude usage with {st.claude_model}: ~{tin:,} in + ~{tout:,} out tokens => approx ${est:.2f} USD (+/-30%)" if todo else "No Claude calls needed.")
        )
        if todo and confirm and (first_run or est > CONFIRM_ABOVE_USD):
            if not confirm("Proceed? [y/N] "):
                out("Cancelled; nothing was sent to Claude and nothing was saved.")
                return None

    # 4. Classify (parallel). Failures are recorded per email and never stop the sync.
    usage = Usage()
    if todo:
        clf = Classifier(cfg.anthropic_api_key, st.claude_model)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(clf.classify, w.msg.sender, w.msg.subject, w.msg.received_at, w.text): w for w in todo
            }
            for n, fut in enumerate(as_completed(futs), 1):
                w = futs[fut]
                r = fut.result()
                usage.add(r.usage)
                w.cls, w.error = r.classification, r.error
                if w.cls and w.msg.internet_message_id:
                    cache.put(cache.key(w.msg.internet_message_id, st.claude_model), w.cls)
                if progress:
                    progress(n, len(todo))
                else:
                    print(f"\r  classified {n}/{len(todo)}", end="", flush=True)
        if not progress:
            print()

    # 4b. Calendar (read-only). A calendar problem is reported but never fails the email sync.
    calendar_events: list[dict] = []
    if st.calendar_check:
        try:
            calendar_events = calsync.fetch(g)
            out(f"Calendar: {len(calendar_events)} event(s) from 6 months ago to 3 months ahead")
        except GraphError as e:
            out(f"Calendar skipped ({str(e)[:120]}). Sign in again with: python -m tracker login")

    # 5. Apply to the database in one transaction, oldest first so statuses build up correctly.
    with db.tx(conn, rollback=dry_run):
        for eid in retry_delete:
            conn.execute("DELETE FROM emails WHERE id=?", (eid,))
        for w in works:
            if w.folder == "inbox" and w.cls is None:
                continue  # don't record failed Inbox lookups; they'll be seen again next time
            conn.execute("SAVEPOINT email")
            try:
                handle_email(conn, w.msg, w.folder, w.cls, st.confidence_threshold, summary, me_addr, w.error)
                conn.execute("RELEASE email")
            except Exception as e:  # noqa: BLE001 - one bad email must never abort the sync
                conn.execute("ROLLBACK TO email")
                conn.execute("RELEASE email")
                summary.errors += 1
                summary.log(f"ERROR    {w.msg.subject[:55]}  (could not save: {type(e).__name__}: {e})")
        _retry_unmatched(conn, summary, me_addr)
        if st.calendar_check:
            calsync.process(conn, calendar_events, me_addr, summary)
        summary.auto_completed = calsync.auto_complete(conn)
        if not dry_run:
            db.kv_set(conn, "sync_state", "folder_id", fid)
            db.kv_set(conn, "sync_state", "folder_name", folder_name)
            db.kv_set(conn, "sync_state", "me_address", me_addr)
            for k, v in new_links.items():
                db.kv_set(conn, "sync_state", f"delta:{k}", v)
            now = db.utcnow()
            if not limit:
                db.kv_set(conn, "sync_state", "last_sync_at", now)
                db.kv_set(conn, "sync_state", "inbox_cursor", now)
            conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, ok, dry_run, summary) VALUES (?,?,?,?,?)",
                (started, now, 1, 0, f"{summary.new_applications} new, {summary.interviews_found} interviews, {summary.needs_review} review"),
            )

    # 6. Report
    if verbose or dry_run:
        out("")
        for line in summary.actions:
            out("  " + line)
    out(
        ("\n=== Dry-run summary (nothing was saved) ===" if dry_run else "\n=== Sync summary ===")
        + f"\n  new applications:      {summary.new_applications}"
        + f"\n  updated applications:  {summary.updated_applications}"
        + f"\n  interviews found:      {summary.interviews_found}"
        + f"\n  calendar matches:      {summary.calendar_matched}   (calendar events needing review: {summary.calendar_review})"
        + f"\n  auto-completed:        {summary.auto_completed} interview(s) whose time has passed"
        + f"\n  needs review:          {summary.needs_review}"
        + f"\n  not application mail:  {summary.other}"
        + f"\n  already known:         {summary.skipped_known}" + (f" ({moved_known} moved into the folder)" if moved_known else "")
        + f"\n  failed (will retry):   {summary.errors}"
        + (f"\n  Claude cost this run:  ${cost_usd(st.claude_model, usage):.3f} USD ({usage.input_tokens:,} in / {usage.output_tokens:,} out)" if todo else "")
    )
    return summary
