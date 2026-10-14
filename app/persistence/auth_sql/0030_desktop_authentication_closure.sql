-- Closing Chrome withdraws only its already accepted authentication child.
-- The original command owns correlation and expiry; no browser authority is copied.
CREATE TABLE desktop_authentication_closures (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_authentication_commands(operation_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'settled'))
);
CREATE TRIGGER desktop_authentication_closure_immutable BEFORE UPDATE ON desktop_authentication_closures
WHEN OLD.operation_id <> NEW.operation_id OR OLD.task_id <> NEW.task_id
  OR OLD.expires_at <> NEW.expires_at OR (OLD.state = 'settled' AND NEW.state <> OLD.state)
BEGIN
    SELECT RAISE(ABORT, 'Authentication closure is immutable');
END;
