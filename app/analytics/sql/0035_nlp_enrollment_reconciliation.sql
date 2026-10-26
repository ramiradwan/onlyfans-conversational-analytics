CREATE TABLE nlp_enrollment_reconciliation (
 account_ref TEXT PRIMARY KEY,
 canonical_store_id TEXT NOT NULL,
 enrollment_id TEXT,
 mode_version INTEGER NOT NULL CHECK(mode_version BETWEEN 0 AND 9007199254740991),
 profile_sha256 TEXT,
 binding_sha256 TEXT NOT NULL
) WITHOUT ROWID;
