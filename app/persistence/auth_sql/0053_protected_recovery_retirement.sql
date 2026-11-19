-- Allow retirement deletion of provably uncommitted protected recoveries while
-- retaining committed, admitted, refused and delivery-attempted audit records.
DROP TRIGGER installation_authority_recovery_no_delete;
CREATE TRIGGER installation_authority_recovery_no_delete
BEFORE DELETE ON installation_authority_recoveries
WHEN OLD.state NOT IN ('reserved','prepare-claimed','prepared')
    OR OLD.delivery_attempts>0
    OR OLD.committed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT,'Authority recovery audit is retained');
END;

CREATE TRIGGER protected_recovery_ceremony_no_delete_committed
BEFORE DELETE ON protected_recovery_ceremonies
WHEN OLD.committed_seen=1
BEGIN
    SELECT RAISE(ABORT,'Committed protected recovery ceremony is retained');
END;

ALTER TABLE protected_recovery_desktop_admissions
    ADD COLUMN expiry_read_claimed INTEGER NOT NULL DEFAULT 0 CHECK (expiry_read_claimed IN (0,1));

DROP TRIGGER protected_recovery_desktop_admission_immutable;
CREATE TRIGGER protected_recovery_desktop_admission_immutable BEFORE UPDATE ON protected_recovery_desktop_admissions
WHEN OLD.recovery_id<>NEW.recovery_id OR OLD.operation_id<>NEW.operation_id OR OLD.journey_id<>NEW.journey_id
    OR OLD.created_at<>NEW.created_at OR NEW.read_attempts<OLD.read_attempts
    OR NEW.read_attempts>OLD.read_attempts+1 OR NEW.next_read_at<OLD.next_read_at
    OR NEW.expiry_read_claimed<OLD.expiry_read_claimed
BEGIN
    SELECT RAISE(ABORT,'Protected recovery desktop admission is immutable');
END;
