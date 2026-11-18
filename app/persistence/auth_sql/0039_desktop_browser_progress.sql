CREATE TABLE desktop_browser_progress (
    journey_id TEXT PRIMARY KEY REFERENCES desktop_setup_journeys(journey_id) ON DELETE CASCADE,
    installation_key_id TEXT NOT NULL,
    installation_key_jkt TEXT NOT NULL,
    observation_json TEXT NOT NULL CHECK (length(observation_json) <= 8192)
);
