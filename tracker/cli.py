from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import auth, db, lock
from .config import LONDON, Config, load_config
from .graph import GraphClient, GraphError


def _confirm_lock(cfg: Config) -> bool:
    other = lock.foreign_lock(cfg.lock_path, cfg.machine_name)
    if other:
        print(
            f"\nWARNING: the database was last opened on '{other['machine']}' at {other['time']} (under 2 hours ago).\n"
            "It may still be running there, or OneDrive may not have finished syncing."
        )
        if input("Continue anyway? [y/N] ").strip().lower() != "y":
            return False
    lock.write_lock(cfg.lock_path, cfg.machine_name)
    return True


def _graph(cfg: Config) -> GraphClient:
    return GraphClient(lambda: auth.get_token(cfg))


def cmd_init_db(cfg: Config, args) -> int:
    if not _confirm_lock(cfg):
        return 1
    try:
        db.connect(cfg.db_path).close()
        print(f"Database ready at {cfg.db_path}")
    finally:
        lock.release_lock(cfg.lock_path, cfg.machine_name)
    return 0


def cmd_login(cfg: Config, args) -> int:
    auth.get_token(cfg, device_code=args.device_code)
    me = _graph(cfg).me()
    print(f"Signed in as {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")
    return 0


def cmd_logout(cfg: Config, args) -> int:
    auth.sign_out(cfg)
    print("Local token cache removed.")
    return 0


def cmd_list_recent(cfg: Config, args) -> int:
    tz = ZoneInfo(LONDON)
    for m in _graph(cfg).recent_messages(args.n):
        when = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).astimezone(tz)
        sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
        print(f"{when:%Y-%m-%d %H:%M}  {sender[:38]:38}  {(m.get('subject') or '')[:70]}")
    return 0


def cmd_find_folder(cfg: Config, args) -> int:
    name = args.name or cfg.applications_folder
    f = _graph(cfg).find_folder(name)
    if not f:
        print(f"No folder named '{name}' found.")
        return 1
    print(f"Found '{f['displayName']}' ({f.get('childFolderCount', 0)} subfolders)")
    return 0


def cmd_sync(cfg: Config, args) -> int:
    from . import sync

    if not args.dry_run and not _confirm_lock(cfg):
        return 1
    try:
        conn = db.connect(cfg.db_path)
        result = sync.run(
            cfg, _graph(cfg), conn,
            dry_run=args.dry_run, folder_name=args.folder, months=args.months, limit=args.limit,
            confirm=(lambda _p: True) if args.yes else (lambda p: input(p).strip().lower() in ("y", "yes")),
            verbose=args.verbose,
        )
        return 0 if result is not None else 1
    finally:
        if not args.dry_run:
            lock.release_lock(cfg.lock_path, cfg.machine_name)


def cmd_serve(cfg: Config, args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .web.app import create_app

    url = f"http://localhost:{cfg.port}"
    print(f"Job Tracker running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="tracker")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the dashboard")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("sync", help="fetch and classify emails")
    s.add_argument("--dry-run", action="store_true", help="print what would happen; save nothing")
    s.add_argument("--folder", help="override the applications folder name")
    s.add_argument("--months", type=int, default=6, help="how far back to look (default 6)")
    s.add_argument("--limit", type=int, help="only the N most recent emails (good for a first trial)")
    s.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    s.add_argument("--verbose", action="store_true", help="print every action (always on for --dry-run)")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("init-db", help="create the database").set_defaults(fn=cmd_init_db)
    s = sub.add_parser("login", help="sign in to Microsoft")
    s.add_argument("--device-code", action="store_true", help="use device-code flow instead of a browser pop-up")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="forget the local sign-in").set_defaults(fn=cmd_logout)
    s = sub.add_parser("list-recent", help="list your most recent emails (connection test)")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(fn=cmd_list_recent)
    s = sub.add_parser("find-folder", help="look up the applications folder by name")
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_find_folder)
    args = p.parse_args(argv)
    cfg = load_config()
    try:
        return args.fn(cfg, args)
    except (auth.AuthError, GraphError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
