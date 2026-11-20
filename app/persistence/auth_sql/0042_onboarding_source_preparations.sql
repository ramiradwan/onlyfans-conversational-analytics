-- Exact source preparation commitments survive presentation cleanup, without
-- retaining browser references, credentials, challenges or signatures.
CREATE TABLE onboarding_source_preparations (
    operation_id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('initial-enrollment','registered-continuation')),
    installation_id TEXT NOT NULL,
    prepare_json TEXT NOT NULL CHECK (length(prepare_json) <= 8192),
    prepare_digest TEXT NOT NULL CHECK (length(prepare_digest) = 64),
    created_at REAL NOT NULL,
    retain_until REAL NOT NULL CHECK (retain_until > created_at)
);

CREATE INDEX onboarding_source_preparation_journey
    ON onboarding_source_preparations(journey_id);

CREATE TRIGGER onboarding_source_preparation_immutable
BEFORE UPDATE ON onboarding_source_preparations
BEGIN
    SELECT RAISE(ABORT, 'Source preparation is immutable');
END;
