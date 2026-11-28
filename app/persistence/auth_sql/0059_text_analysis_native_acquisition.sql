CREATE TABLE text_analysis_native_acquisitions (
    journal_id INTEGER PRIMARY KEY,
    flow_id TEXT NOT NULL UNIQUE,
    scope_sha256 TEXT NOT NULL CHECK (length(scope_sha256) = 64),
    kind TEXT NOT NULL CHECK (kind IN ('preview', 'paid')),
    operation_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    data BLOB NOT NULL CHECK (length(data) BETWEEN 1 AND 98304),
    data_sha256 TEXT NOT NULL CHECK (length(data_sha256) = 64)
);

CREATE INDEX text_analysis_acquisition_scope
ON text_analysis_native_acquisitions(scope_sha256, kind, journal_id DESC);
