-- The initial entry source is durable presentation intent, never account authority.
ALTER TABLE desktop_presentations ADD COLUMN entry_source TEXT NOT NULL DEFAULT 'desktop'
    CHECK (entry_source IN ('desktop', 'extension'));
ALTER TABLE desktop_setup_journeys ADD COLUMN entry_source TEXT NOT NULL DEFAULT 'desktop'
    CHECK (entry_source IN ('desktop', 'extension'));
