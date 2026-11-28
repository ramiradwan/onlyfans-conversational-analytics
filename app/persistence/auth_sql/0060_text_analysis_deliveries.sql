CREATE TABLE text_analysis_deliveries (
    reference_id TEXT PRIMARY KEY REFERENCES text_analysis_authority_references(reference_id),
    operation_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preview', 'license')),
    confirmation BLOB NOT NULL CHECK (length(confirmation) BETWEEN 1 AND 4096),
    issuance_request BLOB NOT NULL CHECK (length(issuance_request) BETWEEN 1 AND 8192),
    response BLOB NOT NULL CHECK (length(response) BETWEEN 1 AND 24576),
    UNIQUE (principal_id, creator_account_id, kind)
) WITHOUT ROWID;

CREATE TABLE text_analysis_release_identities (
    desktop_sha256 TEXT PRIMARY KEY CHECK (length(desktop_sha256) = 64),
    envelope BLOB NOT NULL CHECK (length(envelope) BETWEEN 1 AND 24576)
) WITHOUT ROWID;
