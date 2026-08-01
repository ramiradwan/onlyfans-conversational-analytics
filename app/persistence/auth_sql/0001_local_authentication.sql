CREATE TABLE auth_revocation_state (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    revoked_at TEXT,
    reason TEXT,
    PRIMARY KEY (scope_type, scope_id)
);

CREATE TABLE verified_grant_references (
    reference_id TEXT PRIMARY KEY,
    grant_identifier TEXT NOT NULL UNIQUE,
    grant_type TEXT NOT NULL,
    grant_digest TEXT NOT NULL UNIQUE,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    creator_account_id TEXT,
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (valid_from < expires_at),
    CHECK (grant_type != 'creator_account_binding' OR creator_account_id IS NOT NULL)
);

CREATE TABLE webauthn_credentials (
    credential_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    external_issuer TEXT NOT NULL,
    external_subject TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    signature_count INTEGER NOT NULL DEFAULT 0 CHECK (signature_count >= 0),
    enrolled_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE agent_pairings (
    pairing_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    external_issuer TEXT NOT NULL,
    external_subject TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    key_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT
);

CREATE TABLE agent_pairing_grants (
    pairing_id TEXT NOT NULL REFERENCES agent_pairings(pairing_id),
    grant_reference_id TEXT NOT NULL REFERENCES verified_grant_references(reference_id),
    PRIMARY KEY (pairing_id, grant_reference_id)
);

CREATE TABLE bridge_sessions (
    session_id TEXT PRIMARY KEY,
    secret_digest TEXT NOT NULL UNIQUE,
    csrf_digest TEXT NOT NULL UNIQUE,
    credential_id TEXT NOT NULL REFERENCES webauthn_credentials(credential_id),
    principal_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('creator', 'operator')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (issued_at < expires_at)
);

CREATE TABLE bridge_session_grants (
    session_id TEXT NOT NULL REFERENCES bridge_sessions(session_id),
    grant_reference_id TEXT NOT NULL REFERENCES verified_grant_references(reference_id),
    PRIMARY KEY (session_id, grant_reference_id)
);

CREATE TABLE auth_challenges (
    challenge_id TEXT PRIMARY KEY,
    challenge_kind TEXT NOT NULL CHECK (challenge_kind IN ('webauthn', 'agent')),
    secret_digest TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    credential_id TEXT REFERENCES webauthn_credentials(credential_id),
    relying_party_id TEXT,
    expected_origin TEXT,
    pairing_id TEXT REFERENCES agent_pairings(pairing_id),
    request_method TEXT,
    request_path TEXT,
    request_body_digest TEXT,
    ticket_purpose TEXT CHECK (
        ticket_purpose IS NULL OR
        ticket_purpose IN ('bridge-websocket', 'agent-websocket', 'agent-config')
    ),
    agent_installation_id TEXT,
    creator_account_id TEXT,
    key_id TEXT,
    brain_audience TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT,
    CHECK (issued_at < expires_at),
    CHECK (
        (challenge_kind = 'webauthn' AND
         relying_party_id IS NOT NULL AND expected_origin IS NOT NULL AND
         pairing_id IS NULL AND request_method IS NULL AND request_path IS NULL AND
         request_body_digest IS NULL AND ticket_purpose IS NULL AND
         agent_installation_id IS NULL AND creator_account_id IS NULL AND
         key_id IS NULL AND brain_audience IS NULL)
        OR
        (challenge_kind = 'agent' AND credential_id IS NULL AND
         relying_party_id IS NULL AND expected_origin IS NULL AND
         pairing_id IS NOT NULL AND request_method IS NOT NULL AND
         request_path IS NOT NULL AND request_body_digest IS NOT NULL AND
         ticket_purpose IN ('agent-websocket', 'agent-config') AND
         agent_installation_id IS NOT NULL AND creator_account_id IS NOT NULL AND
         key_id IS NOT NULL AND brain_audience IS NOT NULL)
    )
);

CREATE TABLE runtime_tickets (
    ticket_id TEXT PRIMARY KEY,
    secret_digest TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (
        purpose IN ('bridge-websocket', 'agent-websocket', 'agent-config')
    ),
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('creator', 'operator', 'agent')),
    creator_account_id TEXT NOT NULL,
    parent_session_id TEXT REFERENCES bridge_sessions(session_id),
    parent_pairing_id TEXT REFERENCES agent_pairings(pairing_id),
    expected_bridge_session_id TEXT,
    expected_agent_installation_id TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT,
    CHECK (issued_at < expires_at),
    CHECK (
        (purpose = 'bridge-websocket' AND
         parent_session_id IS NOT NULL AND parent_pairing_id IS NULL AND
         expected_bridge_session_id = parent_session_id AND
         expected_agent_installation_id IS NULL)
        OR
        (purpose IN ('agent-websocket', 'agent-config') AND
         parent_session_id IS NULL AND parent_pairing_id IS NOT NULL AND
         expected_bridge_session_id IS NULL AND
         expected_agent_installation_id IS NOT NULL)
    )
);

CREATE TABLE runtime_ticket_grants (
    ticket_id TEXT NOT NULL REFERENCES runtime_tickets(ticket_id),
    grant_reference_id TEXT NOT NULL REFERENCES verified_grant_references(reference_id),
    PRIMARY KEY (ticket_id, grant_reference_id)
);

CREATE TABLE auth_revocation_bindings (
    object_type TEXT NOT NULL CHECK (
        object_type IN ('bridge_session', 'challenge', 'ticket')
    ),
    object_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    observed_version INTEGER NOT NULL CHECK (observed_version >= 0),
    PRIMARY KEY (object_type, object_id, scope_type, scope_id),
    FOREIGN KEY (scope_type, scope_id)
        REFERENCES auth_revocation_state(scope_type, scope_id)
);

CREATE INDEX auth_challenges_live_digest
    ON auth_challenges(secret_digest, expires_at)
    WHERE consumed_at IS NULL AND invalidated_at IS NULL;
CREATE INDEX runtime_tickets_live_digest
    ON runtime_tickets(secret_digest, expires_at)
    WHERE consumed_at IS NULL AND invalidated_at IS NULL;
CREATE INDEX auth_revocation_bindings_scope
    ON auth_revocation_bindings(scope_type, scope_id);
CREATE INDEX verified_grant_references_expiry
    ON verified_grant_references(expires_at);
