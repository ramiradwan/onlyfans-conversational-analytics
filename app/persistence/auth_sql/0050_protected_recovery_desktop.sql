-- Binds the exact accepted local command to its independent recovery owner.
-- No session credential, signed grant or browser locator is retained here.
CREATE TABLE protected_recovery_desktop_admissions (
    recovery_id TEXT PRIMARY KEY REFERENCES installation_authority_recoveries(recovery_id),
    operation_id TEXT NOT NULL UNIQUE,
    journey_id TEXT NOT NULL REFERENCES desktop_setup_journeys(journey_id),
    created_at REAL NOT NULL,
    read_attempts INTEGER NOT NULL DEFAULT 0 CHECK (read_attempts BETWEEN 0 AND 12),
    next_read_at REAL NOT NULL
);
CREATE TRIGGER protected_recovery_desktop_admission_immutable BEFORE UPDATE ON protected_recovery_desktop_admissions
WHEN OLD.recovery_id<>NEW.recovery_id OR OLD.operation_id<>NEW.operation_id OR OLD.journey_id<>NEW.journey_id
    OR OLD.created_at<>NEW.created_at OR NEW.read_attempts<OLD.read_attempts
    OR NEW.read_attempts>OLD.read_attempts+1 OR NEW.next_read_at<OLD.next_read_at
BEGIN
    SELECT RAISE(ABORT,'Protected recovery desktop admission is immutable');
END;
