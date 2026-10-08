CREATE TABLE onboarding_journeys (
    journey_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    operation_id TEXT UNIQUE,
    installation_id TEXT NOT NULL,
    handoff_reference TEXT,
    handoff_expires_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('new','preparing','prepare-unknown','waiting','completing','unknown','completed','expired','revoked','reauthentication-required')),
    scope_json TEXT,
    prepare_json TEXT,
    reason TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE provisioning_browser_sessions (
    session_digest TEXT PRIMARY KEY,
    csrf_digest TEXT NOT NULL,
    journey_id TEXT NOT NULL REFERENCES onboarding_journeys(journey_id),
    expires_at REAL NOT NULL
);
