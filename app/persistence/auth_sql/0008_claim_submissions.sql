CREATE TABLE provisioning_claim_submissions (
    claim_id TEXT PRIMARY KEY,
    onboarding_transaction_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('submitted', 'consumed', 'refused')),
    outcome TEXT,
    submitted_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (length(claim_id) > 0),
    CHECK (length(onboarding_transaction_id) > 0),
    CHECK (length(organization_id) > 0),
    CHECK (length(installation_id) > 0),
    CHECK ((state = 'submitted') = (resolved_at IS NULL)),
    CHECK ((state = 'refused') = (outcome IS NOT NULL))
);

CREATE INDEX provisioning_claim_submissions_unresolved
    ON provisioning_claim_submissions(submitted_at)
    WHERE state = 'submitted';
