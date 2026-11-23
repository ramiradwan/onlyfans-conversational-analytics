-- A desktop pairing is ready for capture setup after this exact companion
-- applies its initial configuration. This observation grants no authority.
ALTER TABLE agent_pairings ADD COLUMN first_configuration_at TEXT;

-- Older releases recorded the first ready milestone per installation. It can
-- identify a pin if it was the sole candidate for the entire recorded millisecond.
WITH ready AS (
    SELECT installation_id,
           substr(occurred_at, 1, 23) || '000+00:00' AS earliest,
           substr(occurred_at, 1, 23) || '999+00:00' AS latest
    FROM onboarding_progress_outbox
    WHERE milestone = 'first-capture-ready' AND occurred_at LIKE '____-__-__T__:__:__.___Z'
)
UPDATE agent_pairings AS pin SET first_configuration_at = (
    SELECT ready.latest FROM ready
    WHERE ready.installation_id = pin.installation_id
      AND pin.confirmed_at <= ready.earliest
      AND NOT EXISTS (
          SELECT 1 FROM agent_pairings AS other
          WHERE other.installation_id = pin.installation_id
            AND other.pairing_id != pin.pairing_id
            AND other.created_at <= ready.latest
            AND (other.revoked_at IS NULL OR other.revoked_at >= ready.earliest)
      )
) WHERE companion_confirmation_method = 'browser_verified' AND revoked_at IS NULL;
