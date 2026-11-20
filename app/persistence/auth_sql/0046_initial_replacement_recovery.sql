-- Only opaque customer identity, never a session or authentication credential.
CREATE TABLE onboarding_initial_replacement_identities (
    successor_operation_id TEXT PRIMARY KEY REFERENCES onboarding_source_preparations(operation_id) ON DELETE CASCADE,
    predecessor_operation_id TEXT NOT NULL UNIQUE,
    intended_creator_id TEXT NOT NULL,
    expected_customer_id TEXT
);
CREATE TRIGGER onboarding_initial_replacement_identity_immutable
BEFORE UPDATE ON onboarding_initial_replacement_identities
BEGIN
    SELECT RAISE(ABORT, 'Initial successor identity is immutable');
END;

-- A receipt is nonauthorizing; it cannot substitute for original consent scope.
CREATE TABLE onboarding_initial_replacement_recoveries (
    predecessor_operation_id TEXT PRIMARY KEY REFERENCES onboarding_source_replacement_outcomes(predecessor_operation_id) ON DELETE CASCADE,
    desktop_operation_id TEXT NOT NULL UNIQUE,
    provider_origin TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    retry_at REAL NOT NULL,
    receipt_json TEXT CHECK (length(receipt_json)<=2048),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0,1)),
    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0,1)),
    CHECK (completed=0 OR (receipt_json IS NOT NULL AND blocked=0))
);
CREATE TRIGGER onboarding_initial_replacement_recovery_immutable
BEFORE UPDATE ON onboarding_initial_replacement_recoveries
WHEN OLD.predecessor_operation_id<>NEW.predecessor_operation_id
    OR OLD.desktop_operation_id<>NEW.desktop_operation_id
    OR OLD.provider_origin<>NEW.provider_origin
    OR NEW.attempts<OLD.attempts OR NEW.retry_at<OLD.retry_at
    OR (OLD.receipt_json IS NOT NULL AND OLD.receipt_json IS NOT NEW.receipt_json)
    OR NEW.completed<OLD.completed OR NEW.blocked<OLD.blocked
BEGIN
    SELECT RAISE(ABORT, 'Initial original outcome evidence is immutable');
END;
