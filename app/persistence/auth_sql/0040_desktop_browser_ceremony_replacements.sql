-- A replacement reservation is not authorization and never stores browser locators.
-- Retain the original ceremony and the exact caller-selected successor together.
CREATE TABLE desktop_browser_ceremony_replacements (
    predecessor_operation_id TEXT PRIMARY KEY,
    parent_operation_id TEXT NOT NULL REFERENCES desktop_browser_ceremonies(operation_id) ON DELETE CASCADE,
    successor_operation_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL CHECK (length(request_json) <= 8192),
    source_expires_at TEXT NOT NULL,
    created_at REAL NOT NULL,
    CHECK (predecessor_operation_id <> successor_operation_id)
);

CREATE INDEX desktop_browser_ceremony_replacement_parent
    ON desktop_browser_ceremony_replacements(parent_operation_id);

CREATE TRIGGER desktop_browser_ceremony_replacement_immutable
BEFORE UPDATE ON desktop_browser_ceremony_replacements
BEGIN
    SELECT RAISE(ABORT, 'Browser ceremony replacement reservation is immutable');
END;
