-- Reserve one pairing attempt before starting its domain owner. A restart may
-- recover this attempt, but cannot extend its deadline or open a replacement.
CREATE TABLE desktop_pairing_operations (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    pairing_id BLOB NOT NULL UNIQUE CHECK (length(pairing_id) = 32),
    opened_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    CHECK (expires_at > opened_at AND expires_at <= opened_at + 300)
);
CREATE TRIGGER desktop_pairing_operation_immutable BEFORE UPDATE ON desktop_pairing_operations
BEGIN
    SELECT RAISE(ABORT, 'Desktop pairing attempt is immutable');
END;
