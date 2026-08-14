CREATE TABLE provisioning_candidates (
    association_request_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    onboarding_transaction_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'cancelled')),
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK ((state = 'pending') = (resolved_at IS NULL))
);

CREATE UNIQUE INDEX provisioning_candidates_one_pending_per_installation
    ON provisioning_candidates(installation_id)
    WHERE state = 'pending';

CREATE INDEX provisioning_candidates_installation
    ON provisioning_candidates(installation_id);
