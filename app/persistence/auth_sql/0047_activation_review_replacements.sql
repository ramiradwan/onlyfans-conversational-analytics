-- Original review identity is retained independently of its presentation.
-- This is a reservation, not a new quote, consent, or activation.
CREATE TABLE desktop_activation_review_replacements (
    original_operation_id TEXT PRIMARY KEY,
    source_operation_id TEXT NOT NULL UNIQUE REFERENCES onboarding_source_preparations(operation_id) ON DELETE CASCADE,
    activation_operation_id TEXT NOT NULL UNIQUE,
    successor_source_operation_id TEXT NOT NULL UNIQUE,
    successor_activation_operation_id TEXT NOT NULL UNIQUE,
    successor_journey_id TEXT NOT NULL UNIQUE,
    successor_desktop_operation_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL CHECK (length(request_json)<=8192),
    provider_origin TEXT NOT NULL,
    actor_digest TEXT NOT NULL CHECK (length(actor_digest)=64),
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','send-claimed','prepared','refused')),
    refusal_reason TEXT,
    CHECK ((attempts=0)=(status='reserved')),
    CHECK ((status='refused')=(refusal_reason IS NOT NULL)),
    CHECK (source_operation_id<>activation_operation_id AND successor_source_operation_id<>successor_activation_operation_id
        AND successor_source_operation_id NOT IN (source_operation_id,activation_operation_id)
        AND successor_activation_operation_id NOT IN (source_operation_id,activation_operation_id))
);

CREATE TRIGGER desktop_activation_review_replacement_fence
BEFORE UPDATE ON desktop_activation_review_replacements
WHEN OLD.original_operation_id<>NEW.original_operation_id
    OR OLD.source_operation_id<>NEW.source_operation_id OR OLD.activation_operation_id<>NEW.activation_operation_id
    OR OLD.successor_source_operation_id<>NEW.successor_source_operation_id
    OR OLD.successor_activation_operation_id<>NEW.successor_activation_operation_id
    OR OLD.successor_journey_id<>NEW.successor_journey_id OR OLD.successor_desktop_operation_id<>NEW.successor_desktop_operation_id
    OR OLD.request_json<>NEW.request_json
    OR OLD.provider_origin<>NEW.provider_origin OR OLD.actor_digest<>NEW.actor_digest OR OLD.created_at<>NEW.created_at
    OR OLD.status IN ('prepared','refused')
    OR NOT ((OLD.status='reserved' AND NEW.status='send-claimed' AND NEW.attempts=1)
        OR (OLD.status='send-claimed' AND NEW.status='send-claimed' AND NEW.attempts=OLD.attempts+1)
        OR (OLD.status='send-claimed' AND NEW.status IN ('prepared','refused') AND NEW.attempts=OLD.attempts))
BEGIN
    SELECT RAISE(ABORT,'Review replacement identity and outcome are immutable');
END;

CREATE TRIGGER desktop_activation_review_original_journey_fence
BEFORE UPDATE ON onboarding_journeys
WHEN EXISTS (SELECT 1 FROM desktop_activation_review_replacements
             WHERE source_operation_id=OLD.operation_id AND status='prepared')
BEGIN
    SELECT RAISE(ABORT,'Replaced commercial source is immutable');
END;

CREATE TRIGGER desktop_activation_review_original_context_fence
BEFORE UPDATE ON installation_continuation_contexts
WHEN EXISTS (SELECT 1 FROM onboarding_journeys j JOIN desktop_activation_review_replacements r
             ON r.source_operation_id=j.operation_id
             WHERE j.journey_id=OLD.journey_id AND r.status='prepared')
BEGIN
    SELECT RAISE(ABORT,'Replaced commercial continuation is immutable');
END;
