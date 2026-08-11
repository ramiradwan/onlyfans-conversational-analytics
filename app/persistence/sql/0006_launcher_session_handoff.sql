CREATE TABLE launcher_session_handoffs (
    code_hash TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX launcher_session_handoffs_expiry
    ON launcher_session_handoffs (expires_at);
