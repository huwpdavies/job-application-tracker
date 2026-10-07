# Job Application Tracker

A local web app that reads your Outlook.com mailbox and calendar, works out which emails relate to your job
applications (using Claude), and shows them in a dashboard: what you've applied for, what came back, and your
interviews, calls and assessments.

- **Read-only.** It never sends, moves, deletes or edits email or calendar items. The Microsoft permissions it asks for are read-only, and the Graph client in the code has no way to write.
- **Local only.** It runs at `http://localhost:8000` on your machine. Nothing is hosted anywhere.
- **Works on Windows and macOS**, and you can share one database between two laptops through OneDrive.

## Setup (once per machine)

1. Register the app with Microsoft and put the Client ID in `.env`: follow **[SETUP.md](SETUP.md)**.
2. Put your Anthropic API key in `.env` (`ANTHROPIC_API_KEY=`).
3. Create a Python environment **outside** the OneDrive folder (it's OS-specific and huge) and install the requirements. Commands for Windows and macOS are in SETUP.md.
4. Sign in and check the connection:

```
python -m tracker login          # opens your browser; read-only permissions
python -m tracker list-recent    # your 10 newest emails
python -m tracker find-folder    # finds the "Job Applications" folder
```

Each machine signs in separately. Run the commands from the project folder with the environment's Python.

## Everyday use

```
python -m tracker serve          # start the dashboard (opens your browser)
```

Press **Refresh** on the Overview page to pull new mail and calendar events. You can also sync from the terminal:

| Command | What it does |
|---|---|
| `python -m tracker serve` | Starts the dashboard at http://localhost:8000 (`--no-browser` to skip opening it) |
| `python -m tracker sync` | Fetches new mail and calendar events, classifies them, saves everything |
| `python -m tracker sync --dry-run` | Prints what *would* be created or updated, saves nothing |
| `python -m tracker sync --limit 25` | Only the 25 newest emails, handy for a first trial |
| `python -m tracker sync --months 3` | Look back 3 months on the first run (default 6) |
| `python -m tracker sync --verbose` | Print every action |
| `python -m tracker login` / `logout` | Sign in to / forget the Microsoft account on this machine |
| `python -m tracker init-db` | Creates an empty database |

**The first full sync** shows how many emails it found and an estimated Claude cost, then asks you to confirm.
Do that one in the terminal. As a guide, about 470 emails cost roughly $1.40 with the default model. After that,
syncs only look at new mail (Graph "delta" queries, per folder), so they're cheap.

**Re-runs are free.** Every classification is cached on your machine, so a repeated dry-run, an interrupted sync,
or rebuilding the database from scratch doesn't call Claude again. Changing `CLAUDE_MODEL` does (it's part of the cache key).

## What the dashboard shows

- **Overview**: Interviewing / Applied / Rejected counts (the big three), then Needs review, Offer, Withdrawn, No response; a status strip; interviews in the next 14 days; applications to follow up; last sync time; the Refresh button.
- **Applications**: sortable, searchable, filterable table. Click anywhere on a row to open it.
- **Application page**: role, company, status, applied date and job ad URL, plus notes (markdown), "More details" (location, source, job reference, salary, contact), interviews, and the email timeline with links to open each message in Outlook. Edit, snooze, mark "No response", merge into another application, split, or delete.
- **Interviews**: interviews, screening calls, video tests and technical assessments, grouped as New / Scheduled / Completed / Cancelled. Add, edit, mark done, or delete by hand.
- **Google Calendar**: scheduled interviews have an **Add to Google Calendar** button (Interviews page, application page and Overview). It opens Google Calendar's new-event page with the title, time, stage, format and job-ad link filled in; press Save there. There's also an `.ics` download for any other calendar app. The app never signs in to Google or writes to it by itself, and no email text is ever put in an event.
- **Needs review**: things the app wasn't sure about. Accept, Edit then accept, Link to an application, or Dismiss.
- **Settings**: follow-up days, confidence threshold, Claude model, Outlook folder name, Inbox and calendar switches, the sender-domain and keyword lists, and Export / Import.

## How it decides things

- **Which emails**: everything in your "Job Applications" folder and its subfolders is treated as job related. After the first sync it also checks your **Inbox** for emails from senders already linked to an application (shown with a "Not filed yet" tag). Everything else in the Inbox is ignored. Turn this off in Settings.
- **Classification**: Claude returns a category (confirmation, acknowledgement, rejection, interview invite / scheduled / rescheduled / cancelled, assessment, offer, recruiter message, other), the **hiring company** (not the job board), role, location, job reference, source, interview date/time, format, stage, a one-line summary and a confidence score. Anything below the threshold (default 0.7) goes to Needs review instead of changing records.
- **Matching**: normalised company name (so "Acme Ltd" = "Acme"; "Aircall.io, Inc." = "Aircall") plus role title (fuzzy; "Senior" and "Junior" never match each other) plus job reference when present. A confirmation or acknowledgement, an interview or assessment, an offer, or a rejection can **create** an application when none matches (each proves you applied). Recruiter messages and cancellations must attach to an existing one, otherwise they go to review. If several applications at one company fit equally well, it asks you.
- **Status** only moves forward: Applied → Interviewing → Offer/Rejected. A late "we're still reviewing" email can't reset an Interviewing application. Assessments and interview invites set Interviewing. **Withdrawn** and **No response** are only ever set by you.
- **Applied on** is the date of the application's first email.
- **Interviews and the calendar**: your calendar (6 months back, 3 ahead) is matched to interviews by company name in the event or by attendee email domain, within a sensible time window. The calendar's time wins over the email's. An interview-looking event with no matching email goes to Needs review. When an interview's end time passes it becomes **Completed** automatically (with an Undo). Assessments need no date or time.
- **Follow up?** An application still in "Applied" with no new *inbound* email for N days (default 14) is flagged. Snooze it, or mark "No response" to stop flagging. Reminders only appear in the dashboard.
- **Your edits are protected.** Any field you edit by hand is locked (🔒) and later syncs won't overwrite it. Unlock it any time.
- **Times** are stored in UTC and displayed in Europe/London.

## What is sent to Claude

For each email: the sender, subject, received date, and the **plain-text body after stripping HTML, footers and
signatures, shortening links to their host, and truncating to 4,000 characters**. Nothing else is sent: no
attachments, no other mailbox content. Full email bodies are never saved or logged; the database keeps only
Claude's structured answer, the subject, sender and a link to the message in Outlook. The email text is
explicitly treated as untrusted (instructions inside an email are ignored).

## Where your data lives

| What | Where | Synced? |
|---|---|---|
| Database (applications, timeline, interviews, review items, settings) | `DB_PATH` in `.env`, default `./data/tracker.db` | Put it in a OneDrive folder to share between laptops |
| Lock file (machine name and time) | next to the database, `tracker.db.lock` | With the database |
| Microsoft sign-in cache | Windows: `%LOCALAPPDATA%\JobApplicationTracker\` · macOS: `~/Library/Application Support/JobApplicationTracker/` | **No**, per machine |
| Classification cache (no email text) | same folder as the sign-in cache | No, per machine |
| Secrets and settings | `.env` (and optional per-machine `.env.local`) | `.env` is in the project folder; it is git-ignored |

`.env`, `.env.local`, the database, the lock file and the token cache are all in `.gitignore`.

### Using two laptops

1. Put the database in a OneDrive folder: `DB_PATH=C:\Users\you\OneDrive\JobTracker\tracker.db` on one machine, the macOS equivalent in `.env.local` on the other (OneDrive paths differ per OS).
2. Sign in on each machine (`python -m tracker login`).
3. On start-up the app writes the lock file. If the lock was written by the *other* machine less than 2 hours ago, it warns you before opening the database: you may have left it running there, or OneDrive may not have finished syncing. Close the app on one machine before using the other, and let OneDrive finish.
4. SQLite runs in rollback-journal (not WAL) mode precisely so it behaves under file syncing.

### Backup and restore

**Settings → Export my data** saves a JSON file of everything that's *yours*: notes, hand-edited fields and locks,
applications and interviews you added, review decisions, dismissed calendar events, settings, and which emails
belong to which application (so merges and splits can be replayed).

To restore on a fresh database: run `python -m tracker sync` (free, from the cache), then **Settings → Import**.
Everything else is rebuilt from your mailbox.

## Tests

```
python -m pytest                              # ~250 tests, offline, free, about 10 seconds
RUN_LIVE=1 python -m pytest tests/test_live_classification.py   # optional: checks the real model (about 2 cents)
```

Graph and Claude are mocked. The suite covers the pre-filter, company and role matching, status transitions,
timezone handling, text cleaning, the classifier (retries, bad output, model fallbacks), the Graph client
(read-only, paging, retry, delta), calendar matching, the whole sync (first run, incremental, dry-run, failures,
Inbox, moved messages), merge/split/backup, the web pages and actions, and a WCAG AA contrast check of every
colour in the theme. `tests/fixtures.py` holds realistic sample emails: a LinkedIn confirmation, a Workday
confirmation, a rejection, an interview invite with a time, a reschedule, an assessment link, and a job-alert newsletter.

## The look

All colours are CSS variables at the top of `tracker/web/static/style.css`; no template hard-codes a colour.
Change `--accent` (and `--accent-hover`) to re-theme it. `tests/test_theme.py` checks every text/background pair
meets WCAG AA and will tell you if a change breaks contrast.

## Troubleshooting

- **"No module named tracker"**: run commands from the project folder.
- **"AZURE_CLIENT_ID is not set"**: see SETUP.md.
- **A Microsoft sign-in error**: see the troubleshooting list in SETUP.md. `python -m tracker login --device-code` is a fallback.
- **"Internal Server Error" in the browser right after an update**: an old copy of the app is still running. Close the terminal that was running `serve` (or end any leftover `python` processes) and start it again.
- **The first sync says it needs a cost confirmation when you press Refresh**: run `python -m tracker sync` in a terminal for the first full scan.
- **Calendar skipped**: sign in again (`python -m tracker login`) to grant calendar access.

## Project layout

```
tracker/
  cli.py          commands            sync.py       the sync pipeline (also dry-run)
  graph.py        read-only Graph     engine.py     emails -> applications, timeline, interviews
  auth.py         MSAL sign-in        matching.py   company/role normalising and fuzzy matching
  classify.py     Claude + cache      statuses.py   forward-only status rules
  mail.py         message fetching    calsync.py    calendar matching, auto-complete
  textclean.py    HTML -> text        review.py     Needs-review actions
  prefilter.py    domain/keyword      appops.py     merge, split, interviews by hand
  timeutil.py     UTC <-> London      backup.py     export / import
  db.py, schema.sql, settings.py, lock.py, config.py, queries.py
  web/            FastAPI app, templates, theme.css
tests/            pytest suite and fixtures
```
