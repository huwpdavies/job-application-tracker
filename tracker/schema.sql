-- All timestamps are stored as UTC ISO-8601 strings (e.g. 2026-10-06T09:30:00+00:00).

CREATE TABLE applications (
    id               INTEGER PRIMARY KEY,
    company          TEXT NOT NULL,
    company_norm     TEXT NOT NULL,
    role_title       TEXT NOT NULL DEFAULT '',
    role_norm        TEXT NOT NULL DEFAULT '',
    location         TEXT,
    job_reference    TEXT,
    source           TEXT,
    status           TEXT NOT NULL DEFAULT 'Applied'
                     CHECK (status IN ('Applied','Interviewing','Offer','Rejected','Withdrawn','No response')),
    applied_at       TEXT,
    last_activity_at TEXT,
    last_inbound_at  TEXT,            -- follow-up clock counts inbound email only
    snoozed_until    TEXT,
    notes            TEXT NOT NULL DEFAULT '',
    contact_name     TEXT,
    contact_email    TEXT,
    salary_range     TEXT,
    job_url          TEXT,
    locked_fields    TEXT NOT NULL DEFAULT '[]',   -- JSON list of fields edited by hand
    created_manually INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_app_company ON applications(company_norm);

-- Every message ever processed. Moving a message changes its Graph id, so dedupe on internet_message_id too.
CREATE TABLE emails (
    id                  INTEGER PRIMARY KEY,
    graph_id            TEXT NOT NULL UNIQUE,
    internet_message_id TEXT UNIQUE,
    folder              TEXT,                 -- 'applications' | 'inbox'
    sender              TEXT,
    sender_domain       TEXT,
    subject             TEXT,
    received_at         TEXT,
    web_link            TEXT,
    category            TEXT,
    is_job_related      INTEGER,
    confidence          REAL,
    classification      TEXT,                 -- JSON from Claude
    application_id      INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    not_filed_yet       INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL DEFAULT 'processed' CHECK (state IN ('processed','review','dismissed','error')),
    processed_at        TEXT NOT NULL
);
CREATE INDEX idx_email_app ON emails(application_id);
CREATE INDEX idx_email_domain ON emails(sender_domain);

CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    email_id       INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    occurred_at    TEXT NOT NULL,
    category       TEXT NOT NULL,
    summary        TEXT,
    web_link       TEXT
);
CREATE INDEX idx_event_app ON events(application_id, occurred_at);

CREATE TABLE interviews (
    id                INTEGER PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    email_id          INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    kind              TEXT NOT NULL DEFAULT 'interview' CHECK (kind IN ('interview','assessment')),
    status            TEXT NOT NULL DEFAULT 'New' CHECK (status IN ('New','Scheduled','Completed','Cancelled')),
    start_at          TEXT,
    end_at            TEXT,
    format            TEXT,
    stage             TEXT,
    location          TEXT,
    calendar_event_id TEXT,
    auto_completed    INTEGER NOT NULL DEFAULT 0,   -- allows "undo"
    google_added_at   TEXT,                         -- when you last opened Google Calendar for this interview
    locked_fields     TEXT NOT NULL DEFAULT '[]',
    created_manually  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE calendar_events (
    graph_id         TEXT PRIMARY KEY,
    subject          TEXT,
    start_at         TEXT,
    end_at           TEXT,
    organizer        TEXT,
    attendee_domains TEXT,        -- JSON list
    web_link         TEXT,
    interview_id     INTEGER REFERENCES interviews(id) ON DELETE SET NULL,
    state            TEXT NOT NULL DEFAULT 'seen' CHECK (state IN ('seen','matched','review','dismissed'))
);

CREATE TABLE review_items (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('email','calendar')),
    email_id          INTEGER REFERENCES emails(id) ON DELETE CASCADE,
    calendar_event_id TEXT REFERENCES calendar_events(graph_id) ON DELETE CASCADE,
    reason            TEXT,
    suggestion        TEXT,       -- JSON
    state             TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','accepted','dismissed','linked')),
    created_at        TEXT NOT NULL
);

CREATE TABLE settings   (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- sync_state holds the applications-folder id, per-folder delta tokens, last successful sync time, etc.
CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sync_runs  (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    ok INTEGER, dry_run INTEGER NOT NULL DEFAULT 0, summary TEXT
);
