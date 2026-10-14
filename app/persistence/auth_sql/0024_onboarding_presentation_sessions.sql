CREATE TABLE onboarding_presentation_sessions (
    session_digest TEXT PRIMARY KEY REFERENCES provisioning_browser_sessions(session_digest) ON DELETE CASCADE,
    presentation_generation TEXT NOT NULL UNIQUE,
    entry_id TEXT NOT NULL UNIQUE,
    record_journey_id TEXT NOT NULL,
    candidate_journey_id TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('same_target', 'inactive')),
    classification_generation TEXT NOT NULL CHECK (length(classification_generation) = 64),
    candidate_evidence_json TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE onboarding_presentation_retirements (
    journey_id TEXT PRIMARY KEY,
    evidence_json TEXT NOT NULL,
    retired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
