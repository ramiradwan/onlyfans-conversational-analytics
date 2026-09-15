CREATE TABLE hosted_grant_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    grant_reference_id TEXT NOT NULL REFERENCES verified_grant_references(reference_id),
    grant_type TEXT NOT NULL CHECK (grant_type IN (
        'installation_grant', 'membership_snapshot',
        'creator_account_binding', 'license_entitlement'
    )),
    grant_jti TEXT NOT NULL,
    grant_digest TEXT NOT NULL CHECK (length(grant_digest) = 64),
    scope_type TEXT NOT NULL CHECK (scope_type IN (
        'jti', 'installation', 'membership', 'creator-approval', 'entitlement'
    )),
    scope_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    creator_account_id TEXT,
    effective_at INTEGER NOT NULL CHECK (effective_at >= 0),
    recorded_at INTEGER NOT NULL CHECK (recorded_at >= effective_at),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'revoked', 'membership_removed', 'role_reduced',
        'approval_revoked', 'entitlement_inactive'
    )),
    source TEXT NOT NULL CHECK (source = 'hosted_refresh'),
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    denial_issued_at INTEGER NOT NULL CHECK (denial_issued_at >= 0),
    denial_expires_at INTEGER NOT NULL CHECK (denial_expires_at = denial_issued_at + 600),
    retain_through TEXT NOT NULL,
    CHECK (scope_type != 'creator-approval' OR creator_account_id IS NOT NULL)
);

CREATE INDEX hosted_grant_tombstones_jti ON hosted_grant_tombstones(grant_jti);
CREATE INDEX hosted_grant_tombstones_scope ON hosted_grant_tombstones(
    organization_id, installation_id, scope_type, scope_id
);

CREATE INDEX verified_grants_current_refresh
    ON verified_grant_references(verified_at, reference_id)
    WHERE revoked_at IS NULL;

CREATE TRIGGER hosted_grant_tombstones_no_update
BEFORE UPDATE ON hosted_grant_tombstones
BEGIN
    SELECT RAISE(ABORT, 'hosted grant tombstones are append-only');
END;

CREATE TRIGGER hosted_grant_tombstones_no_delete
BEFORE DELETE ON hosted_grant_tombstones
BEGIN
    SELECT RAISE(ABORT, 'hosted grant tombstones are append-only');
END;
