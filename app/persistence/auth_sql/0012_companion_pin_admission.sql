-- Claim a window before doing installation-key work. The first claim is durable;
-- a later request cancels it, even while the first request is signing its offer.
ALTER TABLE companion_pairing_windows ADD COLUMN request_claimed_at TEXT;
ALTER TABLE companion_pairing_windows ADD COLUMN opening_principal_id TEXT;
ALTER TABLE companion_pairing_windows ADD COLUMN opening_session_id TEXT;

DROP TRIGGER companion_pairing_windows_state_guard;
CREATE TRIGGER companion_pairing_windows_state_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN NOT (
    (OLD.state = 'open' AND NEW.state = 'open'
        AND OLD.request_claimed_at IS NULL AND NEW.request_claimed_at IS NOT NULL)
    OR (OLD.state = 'open' AND NEW.state IN (
        'offered', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR (OLD.state = 'offered' AND NEW.state IN (
        'awaiting_confirmation', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR (OLD.state = 'awaiting_confirmation' AND NEW.state IN (
        'confirmed', 'declined', 'cancelled', 'expired', 'revoked'
    ))
    OR (OLD.state = 'confirmed' AND NEW.state IN ('cancelled', 'revoked'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid companion pairing window transition');
END;

CREATE TRIGGER companion_pairing_windows_authority_guard
BEFORE UPDATE ON companion_pairing_windows
WHEN NEW.opening_principal_id IS NOT OLD.opening_principal_id
  OR NEW.opening_session_id IS NOT OLD.opening_session_id
  OR (OLD.installation_grant_reference_id IS NOT NULL AND
      NEW.installation_grant_reference_id IS NOT OLD.installation_grant_reference_id)
  OR (OLD.creator_account_binding_reference_id IS NOT NULL AND
      NEW.creator_account_binding_reference_id IS NOT OLD.creator_account_binding_reference_id)
  OR (OLD.request_claimed_at IS NOT NULL AND
      NEW.request_claimed_at IS NOT OLD.request_claimed_at)
BEGIN
    SELECT RAISE(ABORT, 'companion pairing authority is immutable');
END;

ALTER TABLE agent_pairings ADD COLUMN companion_organization_id TEXT;
ALTER TABLE agent_pairings ADD COLUMN companion_installation_key_id TEXT;
ALTER TABLE agent_pairings ADD COLUMN companion_installation_key_jkt TEXT;
ALTER TABLE agent_pairings ADD COLUMN companion_grant_digest BLOB
    CHECK (companion_grant_digest IS NULL OR
        (typeof(companion_grant_digest) = 'blob' AND length(companion_grant_digest) = 32));
ALTER TABLE agent_pairings ADD COLUMN companion_window_version INTEGER;
ALTER TABLE agent_pairings ADD COLUMN companion_window_expires_at TEXT;

CREATE UNIQUE INDEX agent_pairings_one_companion_pin
    ON agent_pairings(agent_installation_id, creator_account_id)
    WHERE pairing_generation IS NOT NULL AND revoked_at IS NULL;
