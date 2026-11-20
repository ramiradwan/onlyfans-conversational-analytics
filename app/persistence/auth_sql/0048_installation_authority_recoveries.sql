-- Exact protected recovery evidence, separate from ordinary refresh.
-- No JWS, proof, authentication state or new grant is admitted here.
CREATE TABLE installation_authority_recoveries (
    recovery_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    scope_digest TEXT NOT NULL CHECK (length(scope_digest)=64),
    request_json TEXT NOT NULL CHECK (length(request_json)<=8192),
    provider_origin TEXT NOT NULL,
    enrollment_claim_id TEXT NOT NULL REFERENCES provisioning_claim_submissions(claim_id),
    created_at REAL NOT NULL,
    prepare_attempts INTEGER NOT NULL DEFAULT 0 CHECK (prepare_attempts BETWEEN 0 AND 2),
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts BETWEEN 0 AND 3),
    retry_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','prepare-claimed','prepared','committed','refused','admitted')),
    source_reference TEXT,
    source_expires_at TEXT,
    committed_at TEXT,
    refusal_reason TEXT,
    admitted_grants_json TEXT CHECK (length(admitted_grants_json)<=4096),
    CHECK ((state='prepared')=(source_reference IS NOT NULL)),
    CHECK (state<>'prepared' OR source_expires_at IS NOT NULL),
    CHECK ((state='refused')=(refusal_reason IS NOT NULL)),
    CHECK ((state='admitted')=(admitted_grants_json IS NOT NULL)),
    CHECK (state NOT IN ('committed','admitted') OR committed_at IS NOT NULL),
    CHECK (state='reserved' OR prepare_attempts>0)
);
CREATE INDEX authority_recovery_installation ON installation_authority_recoveries(installation_id,created_at);
CREATE UNIQUE INDEX authority_recovery_active ON installation_authority_recoveries(installation_id)
    WHERE state IN ('reserved','prepare-claimed','prepared','committed');
CREATE TRIGGER installation_authority_recovery_immutable BEFORE UPDATE ON installation_authority_recoveries
WHEN OLD.recovery_id<>NEW.recovery_id OR OLD.installation_id<>NEW.installation_id
    OR OLD.scope_digest<>NEW.scope_digest OR OLD.request_json<>NEW.request_json
    OR OLD.provider_origin<>NEW.provider_origin OR OLD.enrollment_claim_id<>NEW.enrollment_claim_id
    OR OLD.created_at<>NEW.created_at OR NEW.prepare_attempts<OLD.prepare_attempts
    OR NEW.prepare_attempts>OLD.prepare_attempts+1 OR NEW.delivery_attempts<OLD.delivery_attempts
    OR NEW.delivery_attempts>OLD.delivery_attempts+1 OR NEW.retry_at<OLD.retry_at
    OR (OLD.source_expires_at IS NOT NULL AND OLD.source_expires_at IS NOT NEW.source_expires_at)
    OR (OLD.source_reference IS NOT NULL AND NEW.state='prepared' AND OLD.source_reference IS NOT NEW.source_reference)
    OR (OLD.committed_at IS NOT NULL AND OLD.committed_at IS NOT NEW.committed_at)
    OR OLD.state IN ('admitted','refused')
    OR NOT ((OLD.state='reserved' AND NEW.state='prepare-claimed' AND NEW.prepare_attempts=1)
        OR (OLD.state='prepare-claimed' AND NEW.state IN ('prepare-claimed','prepared','committed','refused'))
        OR (OLD.state='prepared' AND NEW.state IN ('prepared','committed','refused','admitted'))
        OR (OLD.state='committed' AND NEW.state IN ('committed','refused','admitted')))
BEGIN
    SELECT RAISE(ABORT,'Authority recovery identity and outcome are immutable');
END;
CREATE TRIGGER installation_authority_recovery_no_delete BEFORE DELETE ON installation_authority_recoveries
BEGIN
    SELECT RAISE(ABORT,'Authority recovery audit is retained');
END;
