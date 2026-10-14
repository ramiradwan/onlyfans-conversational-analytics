ALTER TABLE desktop_presentations ADD COLUMN refresh_source TEXT;
CREATE UNIQUE INDEX desktop_presentation_refresh_source ON desktop_presentations(refresh_source) WHERE refresh_source IS NOT NULL;
