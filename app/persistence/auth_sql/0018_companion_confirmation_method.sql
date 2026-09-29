-- Record how the operator's confirmation was verified. Pins committed before
-- this migration were confirmed by comparing codes by eye.
ALTER TABLE agent_pairings ADD COLUMN companion_confirmation_method TEXT
    CHECK (companion_confirmation_method IS NULL
        OR companion_confirmation_method IN ('operator_compared', 'browser_verified'));
