CREATE TABLE onboarding_progress_outbox (
    milestone TEXT PRIMARY KEY CHECK (
        milestone IN ('installed', 'enrolled', 'account-bound', 'first-capture-ready')
    ),
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    onboarding_transaction_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'delivered', 'refused')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT,
    delivered_at TEXT,
    refused_at TEXT,
    CHECK (length(onboarding_transaction_id) > 0),
    CHECK (length(organization_id) > 0),
    CHECK (length(installation_id) > 0),
    CHECK (
        (state = 'pending' AND next_attempt_at IS NOT NULL
            AND delivered_at IS NULL AND refused_at IS NULL)
        OR
        (state = 'delivered' AND next_attempt_at IS NULL
            AND delivered_at IS NOT NULL AND refused_at IS NULL)
        OR
        (state = 'refused' AND next_attempt_at IS NULL
            AND delivered_at IS NULL AND refused_at IS NOT NULL)
    )
);

CREATE INDEX onboarding_progress_outbox_due
    ON onboarding_progress_outbox(next_attempt_at, occurred_at)
    WHERE state = 'pending';
