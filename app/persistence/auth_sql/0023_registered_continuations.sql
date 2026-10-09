ALTER TABLE onboarding_journeys ADD COLUMN kind TEXT NOT NULL DEFAULT 'initial-enrollment'
    CHECK (kind IN ('initial-enrollment', 'registered-continuation'));

CREATE TABLE installation_continuation_contexts (
    journey_id TEXT PRIMARY KEY REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    operation_profile TEXT NOT NULL CHECK (operation_profile = 'urn:bridge-clean:installation-setup-continuation:v2'),
    association_request_id TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    continuation_id TEXT,
    reference TEXT,
    provider_expires_at TEXT,
    epoch TEXT,
    provider_revision INTEGER,
    provider_state TEXT CHECK (provider_state IN ('awaiting-owner', 'awaiting-approval', 'approved',
        'authentication-required', 'expired', 'revoked')),
    CHECK ((continuation_id IS NULL AND provider_expires_at IS NULL AND reference IS NULL)
        OR (continuation_id IS NOT NULL AND provider_expires_at IS NOT NULL)),
    CHECK ((epoch IS NULL AND provider_revision IS NULL AND provider_state IS NULL)
        OR (epoch IS NOT NULL AND provider_revision >= 0 AND provider_state IS NOT NULL))
);

CREATE TABLE onboarding_native_selection_receipts (
    entry_digest TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    journey_id TEXT NOT NULL REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    session_digest TEXT NOT NULL REFERENCES provisioning_browser_sessions(session_digest) ON DELETE CASCADE,
    expires_at REAL NOT NULL,
    previous_journey_id TEXT
);
