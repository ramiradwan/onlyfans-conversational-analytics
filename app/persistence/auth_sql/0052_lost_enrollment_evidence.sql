-- Durable original consumed enrollment receipt, not a grant or actor identity.
-- Retained with the original claim rather than the short-lived setup source.
CREATE TABLE lost_enrollment_evidence (
    claim_id TEXT PRIMARY KEY REFERENCES provisioning_claim_submissions(claim_id) ON DELETE CASCADE,
    installation_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    provider_origin TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK (length(receipt_json)<=2048),
    scope_digest TEXT NOT NULL CHECK (length(scope_digest)=64),
    intended_creator_id TEXT NOT NULL,
    association_request_id TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    bootstrap_refusal TEXT CHECK (bootstrap_refusal IN ('bootstrap_authentication_required',
        'bootstrap_authority_withdrawn','installation_binding_mismatch'))
);
CREATE TRIGGER lost_enrollment_evidence_immutable BEFORE UPDATE ON lost_enrollment_evidence
WHEN OLD.claim_id<>NEW.claim_id OR OLD.installation_id<>NEW.installation_id OR OLD.operation_id<>NEW.operation_id
    OR OLD.provider_origin<>NEW.provider_origin OR OLD.receipt_json<>NEW.receipt_json
    OR OLD.scope_digest<>NEW.scope_digest OR OLD.intended_creator_id<>NEW.intended_creator_id
    OR OLD.association_request_id<>NEW.association_request_id OR OLD.recorded_at<>NEW.recorded_at
    OR (OLD.bootstrap_refusal IS NOT NULL AND OLD.bootstrap_refusal IS NOT NEW.bootstrap_refusal)
BEGIN
    SELECT RAISE(ABORT,'Original enrollment evidence is immutable');
END;
