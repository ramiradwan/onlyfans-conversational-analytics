CREATE TABLE installation_key_reference (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    provider_name TEXT NOT NULL,
    provider_key_name TEXT NOT NULL UNIQUE,
    algorithm TEXT NOT NULL,
    installation_key_id TEXT UNIQUE,
    installation_key_jkt TEXT UNIQUE,
    public_key_jwk TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    CHECK (
        (activated_at IS NULL AND installation_key_id IS NULL AND
         installation_key_jkt IS NULL AND public_key_jwk IS NULL)
        OR
        (activated_at IS NOT NULL AND installation_key_id IS NOT NULL AND
         installation_key_jkt IS NOT NULL AND public_key_jwk IS NOT NULL)
    )
);
