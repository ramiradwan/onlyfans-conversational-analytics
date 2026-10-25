CREATE TABLE text_analysis_authority_references (
    reference_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('license', 'preview')),
    object_id TEXT NOT NULL,
    issuance_id TEXT NOT NULL,
    compact_jws TEXT NOT NULL CHECK (length(compact_jws) <= 16384),
    compact_jws_sha256 TEXT NOT NULL CHECK (length(compact_jws_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    signer_kid TEXT NOT NULL,
    fixture_only INTEGER NOT NULL CHECK (fixture_only IN (0, 1)),
    state TEXT NOT NULL DEFAULT 'staged' CHECK (state IN ('staged', 'active', 'revoked')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(kind, object_id),
    UNIQUE(kind, issuance_id)
) WITHOUT ROWID;
