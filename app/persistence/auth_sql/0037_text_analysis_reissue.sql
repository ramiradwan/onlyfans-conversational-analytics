CREATE TABLE text_analysis_reissue_operations (
    flow_id TEXT PRIMARY KEY,
    scope_sha256 TEXT NOT NULL CHECK (length(scope_sha256) = 64),
    operation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('prepared','awaiting_authorization','authorized','adopted','expired')),
    version INTEGER NOT NULL CHECK (version >= 1),
    metadata BLOB NOT NULL CHECK (length(metadata) <= 16384),
    metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64),
    prepare_request BLOB NOT NULL CHECK (length(prepare_request) BETWEEN 1 AND 8192),
    read_request BLOB NOT NULL CHECK (length(read_request) BETWEEN 1 AND 8192),
    recover_request BLOB NOT NULL CHECK (length(recover_request) BETWEEN 1 AND 8192),
    finalize_request BLOB CHECK (length(finalize_request) BETWEEN 1 AND 8192),
    prepare_response BLOB CHECK (length(prepare_response) BETWEEN 1 AND 4096),
    authorized_response BLOB CHECK (length(authorized_response) BETWEEN 1 AND 24576),
    confirmation BLOB CHECK (length(confirmation) BETWEEN 1 AND 8192),
    result BLOB CHECK (length(result) BETWEEN 1 AND 24576),
    reference_id TEXT REFERENCES text_analysis_authority_references(reference_id),
    CHECK ((state = 'adopted') = (result IS NOT NULL AND reference_id IS NOT NULL))
) WITHOUT ROWID;

CREATE UNIQUE INDEX text_analysis_reissue_active_scope
ON text_analysis_reissue_operations(scope_sha256)
WHERE state IN ('prepared','awaiting_authorization','authorized');
