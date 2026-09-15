CREATE TABLE capability_license_references (
    reference_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL UNIQUE,
    issuance_id TEXT NOT NULL,
    object_digest TEXT NOT NULL UNIQUE CHECK (length(object_digest) = 64),
    compact_jws TEXT NOT NULL,
    subject TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    installation_key_id TEXT NOT NULL,
    installation_key_jkt TEXT NOT NULL,
    seat_id TEXT NOT NULL,
    seat_scope TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (capability = 'analysis-run'),
    licensed_major_version INTEGER NOT NULL CHECK (licensed_major_version >= 1),
    compatible_artifact_family TEXT NOT NULL,
    update_rights INTEGER NOT NULL CHECK (update_rights IN (0, 1)),
    fallback_major_versions TEXT NOT NULL,
    signer_kid TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    verification_source TEXT NOT NULL CHECK (verification_source IN ('production', 'development', 'conformance'))
);

CREATE INDEX capability_license_references_installation
    ON capability_license_references(organization_id, installation_id, seat_id);
