CREATE TABLE onboarding_workspace_recovery (
    previous_journey_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    operation_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('new','preparing','prepare-unknown','waiting','completing','unknown','completed','expired','revoked','reauthentication-required','transfer')),
    scope_json TEXT,
    prepare_json TEXT,
    retired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_journey_id TEXT UNIQUE
);
