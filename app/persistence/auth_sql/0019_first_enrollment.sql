CREATE TABLE first_enrollment_contexts (
    context_digest TEXT PRIMARY KEY,
    challenge_digest TEXT NOT NULL UNIQUE,
    authority_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
