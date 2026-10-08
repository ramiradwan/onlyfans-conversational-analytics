ALTER TABLE onboarding_journeys ADD COLUMN recovery_deadline TEXT;
CREATE TABLE onboarding_transfer_intents (
    journey_id TEXT PRIMARY KEY REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    request_json TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    expires_at REAL NOT NULL,
    continuation_json TEXT,
    phase TEXT NOT NULL DEFAULT 'prepared',
    result_json TEXT
);
CREATE TABLE onboarding_transfer_relays (
    nonce_digest TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE onboarding_transfer_proofs (
    journey_id TEXT NOT NULL REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    challenge_digest TEXT NOT NULL,
    PRIMARY KEY(journey_id, challenge_digest)
);
CREATE TABLE onboarding_uncertain_receipts (
    installation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    scope_json TEXT,
    retired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
