CREATE TABLE account_passkey_authority (
    installation_id TEXT NOT NULL,
    rp_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    installation_key_id TEXT NOT NULL,
    installation_key_jkt TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    external_issuer TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
    binding_record TEXT,
    withdrawal_generation INTEGER,
    withdrawal_digest TEXT,
    pending_withdrawal TEXT,
    PRIMARY KEY (installation_id, rp_id, credential_id),
    CHECK (
        (binding_record IS NOT NULL AND withdrawal_generation IS NULL
            AND withdrawal_digest IS NULL AND pending_withdrawal IS NULL)
        OR
        (binding_record IS NULL AND withdrawal_generation IS NOT NULL AND withdrawal_digest IS NOT NULL
            AND withdrawal_generation > binding_generation
            AND length(withdrawal_digest) = 64)
    )
);

CREATE UNIQUE INDEX account_passkey_one_active_binding
    ON account_passkey_authority(installation_id) WHERE binding_record IS NOT NULL;

CREATE TABLE account_passkey_trust_sequence (
    environment TEXT PRIMARY KEY CHECK (environment IN ('qa', 'production')),
    sequence INTEGER NOT NULL CHECK (sequence > 0)
);
