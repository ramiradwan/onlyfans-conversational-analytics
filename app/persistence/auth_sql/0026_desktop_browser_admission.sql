CREATE TABLE desktop_presentations (
    generation TEXT PRIMARY KEY,
    journey_id TEXT,
    stage TEXT NOT NULL CHECK (stage IN ('setup', 'runtime')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'retired')),
    expires_at REAL NOT NULL
);

CREATE TABLE desktop_browser_tasks (
    task_id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL,
    desktop_generation TEXT NOT NULL REFERENCES desktop_presentations(generation),
    operation_id TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('browser.access', 'browser.identity', 'browser.authentication', 'browser.pairing-confirmation', 'browser.history-permission')),
    scope_digest TEXT NOT NULL CHECK (length(scope_digest) = 64),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    admission_generation TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL CHECK (stage IN ('setup', 'runtime')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'outcome_unknown', 'completed', 'cancelled', 'expired')),
    expires_at REAL NOT NULL
);

CREATE TABLE desktop_admission_entries (
    code_digest TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('desktop', 'browser_task')),
    reference_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('setup', 'runtime')),
    expires_at REAL NOT NULL
);

CREATE TABLE desktop_admission_sessions (
    session_digest TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('desktop', 'browser_task')),
    reference_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('setup', 'runtime')),
    expires_at REAL NOT NULL
);
CREATE INDEX desktop_admission_entry_expiry ON desktop_admission_entries(expires_at);
CREATE INDEX desktop_admission_session_expiry ON desktop_admission_sessions(expires_at);
