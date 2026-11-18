-- Each admitted successor is a new immutable ceremony, never a renewal.
CREATE TABLE desktop_browser_ceremony_successors (
    hosted_operation_id TEXT PRIMARY KEY REFERENCES desktop_browser_ceremony_replacements(successor_operation_id) ON DELETE CASCADE,
    binding_json TEXT NOT NULL CHECK (length(binding_json) <= 8192),
    ceremony_id TEXT NOT NULL UNIQUE,
    provider_expires_at TEXT NOT NULL,
    entry_reference TEXT,
    presentation_state TEXT NOT NULL CHECK (presentation_state IN ('active','retired','expired')),
    domain_status TEXT NOT NULL,
    provider_revision INTEGER
);

CREATE TRIGGER desktop_browser_ceremony_successor_immutable
BEFORE UPDATE ON desktop_browser_ceremony_successors
WHEN OLD.hosted_operation_id <> NEW.hosted_operation_id
  OR OLD.binding_json <> NEW.binding_json OR OLD.ceremony_id <> NEW.ceremony_id
  OR OLD.provider_expires_at <> NEW.provider_expires_at
  OR (OLD.provider_revision IS NOT NULL AND
      (NEW.provider_revision IS NULL OR NEW.provider_revision < OLD.provider_revision))
BEGIN
    SELECT RAISE(ABORT, 'Browser ceremony successor scope is immutable');
END;

-- A reserved but unconfirmed replacement does not become the current ceremony.
CREATE VIEW desktop_browser_ceremony_current AS
WITH lineage AS (
    SELECT operation_id,hosted_operation_id,binding_json,ceremony_id,provider_expires_at,
        entry_reference,presentation_state,domain_status,provider_revision
    FROM desktop_browser_ceremonies
    UNION ALL
    SELECT r.parent_operation_id,s.hosted_operation_id,s.binding_json,s.ceremony_id,
        s.provider_expires_at,s.entry_reference,s.presentation_state,s.domain_status,s.provider_revision
    FROM desktop_browser_ceremony_successors s JOIN desktop_browser_ceremony_replacements r
        ON r.successor_operation_id=s.hosted_operation_id
)
SELECT l.* FROM lineage l WHERE NOT EXISTS (
    SELECT 1 FROM desktop_browser_ceremony_replacements r
    JOIN desktop_browser_ceremony_successors s ON s.hosted_operation_id=r.successor_operation_id
    WHERE r.predecessor_operation_id=l.hosted_operation_id AND r.parent_operation_id=l.operation_id
);

-- A successor can only extend the unique currently retained tip.
CREATE TRIGGER desktop_browser_ceremony_successor_current
BEFORE INSERT ON desktop_browser_ceremony_successors
WHEN NOT EXISTS (
    SELECT 1 FROM desktop_browser_ceremony_replacements r
    JOIN desktop_browser_ceremony_current c ON c.operation_id=r.parent_operation_id
        AND c.hosted_operation_id=r.predecessor_operation_id
    WHERE r.successor_operation_id=NEW.hosted_operation_id
)
BEGIN
    SELECT RAISE(ABORT, 'Browser ceremony successor predecessor changed');
END;
