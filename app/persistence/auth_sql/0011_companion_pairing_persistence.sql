-- Pre-admission companion pairing state is isolated from admitted Agent pairings.
ALTER TABLE agent_pairings ADD COLUMN pairing_generation INTEGER
    CHECK (
        pairing_generation IS NULL OR
        pairing_generation BETWEEN 1 AND 9007199254740991
    );
ALTER TABLE agent_pairings ADD COLUMN pairing_digest BLOB
    CHECK (pairing_digest IS NULL OR (typeof(pairing_digest) = 'blob' AND length(pairing_digest) = 32));
ALTER TABLE agent_pairings ADD COLUMN agent_noise_static_public_key BLOB
    CHECK (
        agent_noise_static_public_key IS NULL OR
        (typeof(agent_noise_static_public_key) = 'blob' AND length(agent_noise_static_public_key) = 32)
    );
ALTER TABLE agent_pairings ADD COLUMN brain_noise_static_public_key BLOB
    CHECK (
        brain_noise_static_public_key IS NULL OR
        (typeof(brain_noise_static_public_key) = 'blob' AND length(brain_noise_static_public_key) = 32)
    );
ALTER TABLE agent_pairings ADD COLUMN protected_brain_noise_static_private_key BLOB
    CHECK (
        protected_brain_noise_static_private_key IS NULL OR
        (
            typeof(protected_brain_noise_static_private_key) = 'blob' AND
            length(protected_brain_noise_static_private_key) BETWEEN 1 AND 4096
        )
    );
ALTER TABLE agent_pairings ADD COLUMN confirmation_principal_id TEXT;
ALTER TABLE agent_pairings ADD COLUMN confirmation_session_id TEXT;
ALTER TABLE agent_pairings ADD COLUMN confirmed_at TEXT;

CREATE TABLE companion_pairing_generations (
    installation_id TEXT PRIMARY KEY NOT NULL,
    highest_generation INTEGER NOT NULL
        CHECK (highest_generation BETWEEN 1 AND 9007199254740991)
);

CREATE TRIGGER companion_pairing_generations_monotonic
BEFORE UPDATE ON companion_pairing_generations
WHEN NEW.installation_id IS NOT OLD.installation_id
  OR NEW.highest_generation != OLD.highest_generation + 1
BEGIN
    SELECT RAISE(ABORT, 'companion pairing generation must advance by one');
END;

CREATE TABLE companion_pairing_windows (
    pairing_id BLOB PRIMARY KEY NOT NULL
        CHECK (typeof(pairing_id) = 'blob' AND length(pairing_id) = 32),
    installation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    generation INTEGER NOT NULL
        CHECK (generation BETWEEN 1 AND 9007199254740991),
    state TEXT NOT NULL CHECK (
        state IN (
            'open',
            'offered',
            'awaiting_confirmation',
            'confirmed',
            'declined',
            'cancelled',
            'expired',
            'revoked'
        )
    ),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    opened_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    brain_nonce BLOB NOT NULL
        CHECK (typeof(brain_nonce) = 'blob' AND length(brain_nonce) = 32),
    brain_noise_public_key BLOB NOT NULL
        CHECK (typeof(brain_noise_public_key) = 'blob' AND length(brain_noise_public_key) = 32),
    wrapped_brain_noise_private_key BLOB
        CHECK (
            wrapped_brain_noise_private_key IS NULL OR
            (
                typeof(wrapped_brain_noise_private_key) = 'blob' AND
                length(wrapped_brain_noise_private_key) BETWEEN 1 AND 4096
            )
        ),
    agent_installation_id TEXT,
    agent_identity_jwk TEXT
        CHECK (agent_identity_jwk IS NULL OR length(agent_identity_jwk) BETWEEN 1 AND 4096),
    agent_identity_thumbprint TEXT
        CHECK (
            agent_identity_thumbprint IS NULL OR
            length(agent_identity_thumbprint) BETWEEN 1 AND 256
        ),
    agent_noise_public_key BLOB
        CHECK (
            agent_noise_public_key IS NULL OR
            (typeof(agent_noise_public_key) = 'blob' AND length(agent_noise_public_key) = 32)
        ),
    agent_nonce BLOB
        CHECK (
            agent_nonce IS NULL OR
            (typeof(agent_nonce) = 'blob' AND length(agent_nonce) = 32)
        ),
    pairing_digest BLOB
        CHECK (
            pairing_digest IS NULL OR
            (typeof(pairing_digest) = 'blob' AND length(pairing_digest) = 32)
        ),
    grant_digest BLOB
        CHECK (
            grant_digest IS NULL OR
            (typeof(grant_digest) = 'blob' AND length(grant_digest) = 32)
        ),
    installation_grant_reference_id TEXT
        REFERENCES verified_grant_references(reference_id),
    creator_account_binding_reference_id TEXT
        REFERENCES verified_grant_references(reference_id),
    offered_at TEXT,
    awaiting_confirmation_at TEXT,
    confirmation_principal_id TEXT,
    confirmation_session_id TEXT,
    confirmed_at TEXT,
    terminal_at TEXT,
    terminal_reason TEXT CHECK (
        terminal_reason IS NULL OR length(terminal_reason) BETWEEN 1 AND 256
    ),
    CHECK (opened_at < expires_at),
    CHECK (
        state NOT IN ('offered', 'awaiting_confirmation', 'confirmed')
        OR (
            agent_installation_id IS NOT NULL AND
            agent_identity_jwk IS NOT NULL AND
            agent_identity_thumbprint IS NOT NULL AND
            agent_noise_public_key IS NOT NULL AND
            agent_nonce IS NOT NULL AND
            pairing_digest IS NOT NULL AND
            grant_digest IS NOT NULL AND
            installation_grant_reference_id IS NOT NULL AND
            creator_account_binding_reference_id IS NOT NULL AND
            offered_at IS NOT NULL
        )
    ),
    CHECK (
        state NOT IN ('awaiting_confirmation', 'confirmed')
        OR awaiting_confirmation_at IS NOT NULL
    ),
    CHECK (
        (state = 'confirmed' AND
            confirmation_principal_id IS NOT NULL AND
            confirmation_session_id IS NOT NULL AND
            confirmed_at IS NOT NULL AND
            terminal_at IS NULL)
        OR
        (state != 'confirmed' AND
            confirmation_principal_id IS NULL AND
            confirmation_session_id IS NULL AND
            confirmed_at IS NULL)
    ),
    CHECK (
        state NOT IN ('declined', 'cancelled', 'expired', 'revoked')
        OR (
            terminal_at IS NOT NULL AND
            wrapped_brain_noise_private_key IS NULL
        )
    ),
    CHECK (
        state IN ('declined', 'cancelled', 'expired', 'revoked')
        OR wrapped_brain_noise_private_key IS NOT NULL
    ),
    CHECK (
        state IN ('declined', 'cancelled', 'expired', 'revoked')
        OR terminal_at IS NULL
    ),
    CHECK (
        state IN ('declined', 'cancelled', 'expired', 'revoked')
        OR terminal_reason IS NULL
    ),
    CHECK (
        installation_grant_reference_id IS NULL
        OR creator_account_binding_reference_id IS NULL
        OR installation_grant_reference_id != creator_account_binding_reference_id
    )
);

CREATE UNIQUE INDEX companion_pairing_windows_one_live
    ON companion_pairing_windows(installation_id)
    WHERE state IN ('open', 'offered', 'awaiting_confirmation');

CREATE INDEX companion_pairing_windows_expiry
    ON companion_pairing_windows(state, expires_at);

CREATE TRIGGER companion_pairing_windows_version_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN NEW.version != OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'companion pairing window version must increase by one');
END;

CREATE TRIGGER companion_pairing_windows_identity_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN NEW.pairing_id IS NOT OLD.pairing_id
  OR NEW.installation_id IS NOT OLD.installation_id
  OR NEW.creator_account_id IS NOT OLD.creator_account_id
  OR NEW.generation != OLD.generation
  OR NEW.opened_at IS NOT OLD.opened_at
  OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.brain_nonce IS NOT OLD.brain_nonce
  OR NEW.brain_noise_public_key IS NOT OLD.brain_noise_public_key
BEGIN
    SELECT RAISE(ABORT, 'companion pairing window identity is immutable');
END;

CREATE TRIGGER companion_pairing_windows_state_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN NOT (
    (OLD.state = 'open' AND NEW.state IN (
        'offered', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR
    (OLD.state = 'offered' AND NEW.state IN (
        'awaiting_confirmation', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR
    (OLD.state = 'awaiting_confirmation' AND NEW.state IN (
        'confirmed', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR
    (OLD.state = 'confirmed' AND NEW.state IN ('cancelled', 'revoked'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid companion pairing window transition');
END;

CREATE TRIGGER companion_pairing_windows_offer_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN OLD.state IN ('offered', 'awaiting_confirmation')
 AND (
    NEW.agent_installation_id IS NOT OLD.agent_installation_id
    OR NEW.agent_identity_jwk IS NOT OLD.agent_identity_jwk
    OR NEW.agent_identity_thumbprint IS NOT OLD.agent_identity_thumbprint
    OR NEW.agent_noise_public_key IS NOT OLD.agent_noise_public_key
    OR NEW.agent_nonce IS NOT OLD.agent_nonce
    OR NEW.pairing_digest IS NOT OLD.pairing_digest
    OR NEW.grant_digest IS NOT OLD.grant_digest
    OR NEW.installation_grant_reference_id IS NOT OLD.installation_grant_reference_id
    OR NEW.creator_account_binding_reference_id IS NOT OLD.creator_account_binding_reference_id
    OR NEW.offered_at IS NOT OLD.offered_at
 )
BEGIN
    SELECT RAISE(ABORT, 'companion pairing offer is immutable');
END;
