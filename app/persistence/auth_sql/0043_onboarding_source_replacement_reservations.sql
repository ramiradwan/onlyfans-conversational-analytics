-- Retain the authoritative prepare outcome coordinates, never the browser entry.
CREATE TABLE onboarding_source_prepared_receipts (
    operation_id TEXT PRIMARY KEY REFERENCES onboarding_source_preparations(operation_id) ON DELETE CASCADE,
    provider_expires_at REAL NOT NULL,
    continuation_id TEXT
);

CREATE TRIGGER onboarding_source_prepared_receipt_immutable
BEFORE UPDATE ON onboarding_source_prepared_receipts
BEGIN
    SELECT RAISE(ABORT, 'Source prepared receipt is immutable');
END;

-- A reservation is not an admitted source or authority. A send claim only
-- records that the outcome may become uncertain. It cannot be reset.
CREATE TABLE onboarding_source_replacement_reservations (
    predecessor_operation_id TEXT PRIMARY KEY REFERENCES onboarding_source_preparations(operation_id) ON DELETE CASCADE,
    successor_operation_id TEXT NOT NULL UNIQUE,
    creator_account_id TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK (length(request_json) <= 8192),
    created_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','send-claimed')),
    CHECK (predecessor_operation_id <> successor_operation_id)
);

CREATE TRIGGER onboarding_source_replacement_reservation_fence
BEFORE UPDATE ON onboarding_source_replacement_reservations
WHEN OLD.predecessor_operation_id <> NEW.predecessor_operation_id
  OR OLD.successor_operation_id <> NEW.successor_operation_id
  OR OLD.creator_account_id <> NEW.creator_account_id
  OR OLD.request_json <> NEW.request_json
  OR OLD.created_at <> NEW.created_at
  OR OLD.state <> 'reserved' OR NEW.state <> 'send-claimed'
BEGIN
    SELECT RAISE(ABORT, 'Source replacement reservation is immutable');
END;
