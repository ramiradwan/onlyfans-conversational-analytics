ALTER TABLE desktop_setup_journeys ADD COLUMN account_passkey_paused
    INTEGER NOT NULL DEFAULT 0 CHECK (account_passkey_paused IN (0, 1));

-- Working evidence only. A retained row must be reconciled with the Provider.
CREATE TABLE desktop_account_passkey_attempts (
    journey_id TEXT PRIMARY KEY REFERENCES desktop_setup_journeys(journey_id) ON DELETE CASCADE,
    scope_digest TEXT NOT NULL CHECK (length(scope_digest) = 64),
    enrollment_id TEXT NOT NULL UNIQUE,
    initial_access_nonce TEXT NOT NULL CHECK (length(initial_access_nonce) = 43),
    source_deadline REAL NOT NULL,
    retire_at REAL NOT NULL CHECK (retire_at <= source_deadline),
    attempt_json TEXT CHECK (length(attempt_json) <= 2048),
    credential_id TEXT CHECK (length(credential_id) BETWEEN 1 AND 1364),
    public_key BLOB CHECK (length(public_key) BETWEEN 1 AND 1024),
    CHECK ((credential_id IS NULL AND public_key IS NULL)
        OR (attempt_json IS NOT NULL AND credential_id IS NOT NULL AND public_key IS NOT NULL))
);
