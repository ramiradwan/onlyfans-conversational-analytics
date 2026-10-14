ALTER TABLE capability_license_pending_deliveries ADD COLUMN provider_origin TEXT;

CREATE TABLE capability_license_redemption_journal (
    provider_origin TEXT NOT NULL,
    continuation_digest TEXT NOT NULL CHECK (length(continuation_digest) = 64),
    scope_json TEXT NOT NULL CHECK (length(scope_json) <= 2048),
    request_json TEXT NOT NULL CHECK (length(request_json) <= 8192),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    idempotency_key TEXT NOT NULL UNIQUE,
    response_status INTEGER CHECK (response_status IN (200, 201)),
    response_json TEXT CHECK (length(response_json) <= 8192),
    response_digest TEXT CHECK (length(response_digest) = 64),
    terminal_status INTEGER CHECK (terminal_status IN (404,410)),
    terminal_json TEXT CHECK (length(terminal_json) <= 8192),
    terminal_digest TEXT CHECK (length(terminal_digest) = 64),
    delivery_operation TEXT CHECK (delivery_operation IN ('activate','finalize')),
    authority_reference_id TEXT,
    response_object_digest TEXT CHECK (length(response_object_digest) = 64),
    completed_reference_id TEXT,
    PRIMARY KEY (provider_origin, continuation_digest),
    CHECK ((response_status IS NULL) = (response_json IS NULL)),
    CHECK ((response_json IS NULL) = (response_digest IS NULL)),
    CHECK ((terminal_status IS NULL) = (terminal_json IS NULL)),
    CHECK ((terminal_json IS NULL) = (terminal_digest IS NULL)),
    CHECK (terminal_json IS NULL OR response_json IS NULL),
    CHECK ((delivery_operation IS NULL) = (authority_reference_id IS NULL)),
    CHECK (authority_reference_id IS NULL OR response_json IS NOT NULL),
    CHECK (response_object_digest IS NULL OR authority_reference_id IS NOT NULL),
    CHECK (completed_reference_id IS NULL OR response_object_digest IS NOT NULL)
);
CREATE TRIGGER capability_license_redemption_journal_immutable
BEFORE UPDATE ON capability_license_redemption_journal
WHEN OLD.provider_origin <> NEW.provider_origin
  OR OLD.continuation_digest <> NEW.continuation_digest OR OLD.scope_json <> NEW.scope_json
  OR OLD.request_json <> NEW.request_json OR OLD.request_digest <> NEW.request_digest
  OR OLD.idempotency_key <> NEW.idempotency_key
  OR (OLD.terminal_json IS NOT NULL AND (OLD.terminal_json IS NOT NEW.terminal_json
      OR OLD.terminal_status IS NOT NEW.terminal_status OR OLD.terminal_digest IS NOT NEW.terminal_digest))
  OR (OLD.authority_reference_id IS NOT NULL AND (NEW.authority_reference_id IS NULL
      OR OLD.authority_reference_id <> NEW.authority_reference_id OR OLD.delivery_operation IS NOT NEW.delivery_operation))
  OR (OLD.response_object_digest IS NOT NULL AND (NEW.response_object_digest IS NULL
      OR OLD.response_object_digest <> NEW.response_object_digest))
  OR (OLD.completed_reference_id IS NOT NULL AND (NEW.completed_reference_id IS NULL
      OR OLD.completed_reference_id <> NEW.completed_reference_id))
  OR (OLD.response_json IS NOT NULL AND (NEW.response_json IS NULL
      OR OLD.response_json <> NEW.response_json OR OLD.response_status <> NEW.response_status
      OR OLD.response_digest <> NEW.response_digest))
BEGIN
    SELECT RAISE(ABORT, 'Redemption journal is immutable');
END;

-- This owner record survives the lifetime of the desktop/browser presentation.
CREATE TABLE capability_license_activation_deliveries (
    activation_operation_id TEXT PRIMARY KEY,
    provider_origin TEXT NOT NULL,
    binding_json TEXT NOT NULL CHECK (length(binding_json) <= 2048),
    activation_committed INTEGER NOT NULL DEFAULT 0 CHECK (activation_committed IN (0,1)),
    current_generation INTEGER NOT NULL DEFAULT 0 CHECK (current_generation BETWEEN 0 AND 9007199254740991),
    requested_generation INTEGER CHECK (requested_generation BETWEEN 1 AND 9007199254740991),
    recover_request_json TEXT CHECK (length(recover_request_json) <= 4096),
    accepted_result_json TEXT CHECK (length(accepted_result_json) <= 4096),
    redemption_json TEXT CHECK (length(redemption_json) <= 2048),
    retry_at REAL,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count BETWEEN 0 AND 6),
    completed_reference_id TEXT,
    CHECK ((requested_generation IS NULL) = (recover_request_json IS NULL))
);
CREATE TRIGGER capability_license_activation_delivery_scope_immutable
BEFORE UPDATE ON capability_license_activation_deliveries
WHEN OLD.activation_operation_id <> NEW.activation_operation_id
  OR OLD.provider_origin <> NEW.provider_origin OR OLD.binding_json <> NEW.binding_json
  OR NEW.activation_committed < OLD.activation_committed
  OR NEW.current_generation < OLD.current_generation
  OR (OLD.completed_reference_id IS NOT NULL AND (NEW.completed_reference_id IS NULL
      OR NEW.completed_reference_id <> OLD.completed_reference_id))
BEGIN
    SELECT RAISE(ABORT, 'Activation delivery scope is immutable');
END;

-- The verified-authority transaction settles both first persistence and exact replay.
DROP TRIGGER capability_license_pending_delivery_completed;

CREATE TRIGGER capability_license_delivery_origin_immutable
BEFORE UPDATE ON capability_license_pending_deliveries
WHEN OLD.provider_origin IS NOT NEW.provider_origin
BEGIN
    SELECT RAISE(ABORT, 'Delivery Provider is immutable');
END;
