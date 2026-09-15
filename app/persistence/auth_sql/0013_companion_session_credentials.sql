-- Credentials minted inside one authenticated Noise session never authorize
-- plaintext transports or another Noise handshake.
ALTER TABLE auth_challenges ADD COLUMN companion_session_id TEXT;
ALTER TABLE runtime_tickets ADD COLUMN companion_session_id TEXT;
CREATE INDEX auth_challenges_companion_session ON auth_challenges(companion_session_id)
    WHERE companion_session_id IS NOT NULL;
CREATE INDEX runtime_tickets_companion_session ON runtime_tickets(companion_session_id)
    WHERE companion_session_id IS NOT NULL;
