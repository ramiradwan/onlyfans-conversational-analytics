-- Presentation records only. The recovery owner retains and admits grants.
CREATE TABLE protected_recovery_ceremonies (
    recovery_id TEXT PRIMARY KEY REFERENCES installation_authority_recoveries(recovery_id),
    operation_id TEXT NOT NULL UNIQUE,
    binding_json TEXT NOT NULL CHECK (length(binding_json)<=8192),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest)=64),
    created_at REAL NOT NULL,
    prepare_claimed INTEGER NOT NULL DEFAULT 0 CHECK (prepare_claimed IN (0,1)),
    ceremony_id TEXT UNIQUE,
    expires_at TEXT,
    revision INTEGER CHECK (revision>=0),
    presentation_state TEXT CHECK (presentation_state IN ('active','retired','expired')),
    domain_status TEXT CHECK (domain_status IN ('awaiting-authentication','committed','unavailable','revoked')),
    committed_seen INTEGER NOT NULL DEFAULT 0 CHECK (committed_seen IN (0,1)),
    entry_reference TEXT,
    entry_digest TEXT CHECK (length(entry_digest)=64),
    cancel_claimed INTEGER NOT NULL DEFAULT 0 CHECK (cancel_claimed IN (0,1)),
    projection_json TEXT CHECK (length(projection_json)<=8192),
    CHECK ((ceremony_id IS NULL)=(expires_at IS NULL)),
    CHECK (ceremony_id IS NULL OR prepare_claimed=1),
    CHECK (entry_reference IS NULL OR (presentation_state='active' AND domain_status='awaiting-authentication' AND cancel_claimed=0)),
    CHECK (projection_json IS NULL OR entry_reference IS NOT NULL)
);
CREATE TRIGGER protected_recovery_ceremony_fence BEFORE UPDATE ON protected_recovery_ceremonies
WHEN OLD.recovery_id<>NEW.recovery_id OR OLD.operation_id<>NEW.operation_id
    OR OLD.binding_json<>NEW.binding_json OR OLD.binding_digest<>NEW.binding_digest OR OLD.created_at<>NEW.created_at
    OR NEW.prepare_claimed<OLD.prepare_claimed OR NEW.cancel_claimed<OLD.cancel_claimed
    OR NEW.committed_seen<OLD.committed_seen
    OR (OLD.entry_digest IS NOT NULL AND OLD.entry_digest IS NOT NEW.entry_digest)
    OR (OLD.ceremony_id IS NOT NULL AND OLD.ceremony_id IS NOT NEW.ceremony_id)
    OR (OLD.expires_at IS NOT NULL AND OLD.expires_at IS NOT NEW.expires_at)
    OR (OLD.revision IS NOT NULL AND (NEW.revision IS NULL OR NEW.revision<OLD.revision))
    OR (OLD.presentation_state IN ('retired','expired') AND NEW.presentation_state='active')
    OR (OLD.presentation_state='expired' AND NEW.presentation_state IS NOT 'expired')
    OR (OLD.committed_seen=1 AND NEW.domain_status='awaiting-authentication')
    OR (OLD.domain_status='revoked' AND NEW.domain_status IS NOT 'revoked')
BEGIN
    SELECT RAISE(ABORT,'Protected recovery ceremony identity is immutable');
END;
CREATE TABLE protected_recovery_authentication_children (
    attempt_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL REFERENCES protected_recovery_ceremonies(recovery_id),
    previous_attempt_id TEXT REFERENCES protected_recovery_authentication_children(attempt_id),
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 32),
    request_json TEXT NOT NULL CHECK (length(request_json)<=8192),
    state TEXT NOT NULL CHECK (state IN ('select-claimed','selected','cancel-claimed','retired','admitted')),
    open_claimed INTEGER NOT NULL DEFAULT 0 CHECK (open_claimed IN (0,1)),
    UNIQUE(recovery_id,sequence),
    UNIQUE(previous_attempt_id),
    CHECK ((sequence=1)=(previous_attempt_id IS NULL))
);
CREATE TRIGGER protected_recovery_child_fence BEFORE UPDATE ON protected_recovery_authentication_children
WHEN OLD.attempt_id<>NEW.attempt_id OR OLD.recovery_id<>NEW.recovery_id OR OLD.previous_attempt_id IS NOT NEW.previous_attempt_id
    OR OLD.sequence<>NEW.sequence OR OLD.request_json<>NEW.request_json OR NEW.open_claimed<OLD.open_claimed
    OR NOT ((OLD.state='select-claimed' AND NEW.state IN ('select-claimed','selected','retired','admitted'))
        OR (OLD.state='selected' AND NEW.state IN ('selected','cancel-claimed','retired','admitted'))
        OR (OLD.state='cancel-claimed' AND NEW.state IN ('cancel-claimed','retired','admitted'))
        OR (OLD.state='retired' AND NEW.state='retired')
        OR (OLD.state='admitted' AND NEW.state='admitted'))
BEGIN
    SELECT RAISE(ABORT,'Protected authentication attempt identity is immutable');
END;
