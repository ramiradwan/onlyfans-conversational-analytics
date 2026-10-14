-- One installation-owned authority, independent of creator accounts and cache.
CREATE TABLE signing_publication_authority (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state_json TEXT,
    state_sha256 TEXT,
    initialized INTEGER NOT NULL DEFAULT 0 CHECK (initialized IN (0, 1)),
    CHECK ((state_json IS NULL) = (state_sha256 IS NULL))
);
-- NULL is genuine first use only; disappearance of this row is corruption.
INSERT INTO signing_publication_authority(singleton) VALUES (1);
