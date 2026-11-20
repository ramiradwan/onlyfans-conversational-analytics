-- Only the exact original source and activation may produce terminal evidence.
-- No browser locator, credential, proof, redemption or offer is stored here.
CREATE TABLE desktop_activation_reconciliations (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_activation_sources(operation_id) ON DELETE CASCADE,
    source_operation_id TEXT NOT NULL REFERENCES onboarding_source_preparations(operation_id) ON DELETE CASCADE,
    activation_operation_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL CHECK (length(request_digest)=64),
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    retry_at REAL NOT NULL,
    status TEXT CHECK (status IN ('committed','expired-uncommitted','refused')),
    observed_at REAL,
    CHECK ((status IS NULL) = (observed_at IS NULL))
);

CREATE TRIGGER desktop_activation_reconciliation_immutable
BEFORE UPDATE ON desktop_activation_reconciliations
WHEN OLD.operation_id<>NEW.operation_id
    OR OLD.source_operation_id<>NEW.source_operation_id
    OR OLD.activation_operation_id<>NEW.activation_operation_id
    OR OLD.request_digest<>NEW.request_digest
    OR NEW.attempts<OLD.attempts
    OR NEW.retry_at<OLD.retry_at
    OR (OLD.status IS NOT NULL AND
        (OLD.status IS NOT NEW.status OR OLD.observed_at IS NOT NEW.observed_at
         OR OLD.attempts<>NEW.attempts OR OLD.retry_at<>NEW.retry_at))
BEGIN
    SELECT RAISE(ABORT, 'Original activation reconciliation is immutable');
END;
