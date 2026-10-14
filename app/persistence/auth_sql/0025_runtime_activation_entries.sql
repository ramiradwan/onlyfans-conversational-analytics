CREATE TABLE runtime_activation_workspace_receipts (
    receipt_id TEXT PRIMARY KEY,
    previous_journey_id TEXT NOT NULL,
    journey_id TEXT NOT NULL REFERENCES onboarding_journeys(journey_id) ON DELETE CASCADE,
    actor_digest TEXT NOT NULL CHECK (length(actor_digest) = 64),
    selection_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    UNIQUE (previous_journey_id, journey_id, actor_digest)
);
