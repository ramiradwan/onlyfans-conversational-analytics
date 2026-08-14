CREATE TABLE authorized_account_bindings (
    creator_account_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    platform_creator_id TEXT NOT NULL,
    association_request_id TEXT NOT NULL UNIQUE
        REFERENCES provisioning_candidates(association_request_id),
    grant_bundle_sha256 TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (length(creator_account_id) > 0),
    CHECK (length(installation_id) > 0),
    CHECK (length(platform_creator_id) > 0),
    CHECK (length(grant_bundle_sha256) = 64)
);

CREATE TABLE authorized_account_binding_grants (
    creator_account_id TEXT NOT NULL
        REFERENCES authorized_account_bindings(creator_account_id),
    grant_reference_id TEXT NOT NULL
        REFERENCES verified_grant_references(reference_id),
    PRIMARY KEY (creator_account_id, grant_reference_id)
);

CREATE INDEX authorized_account_bindings_live
    ON authorized_account_bindings(installation_id)
    WHERE revoked_at IS NULL;
