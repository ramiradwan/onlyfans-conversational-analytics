CREATE TABLE derived_deletion_work (
 creator_account_id TEXT PRIMARY KEY, target_revision INTEGER NOT NULL,
 completed_revision INTEGER NOT NULL DEFAULT 0, attempted_at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
INSERT INTO derived_deletion_work(creator_account_id,target_revision)
 SELECT creator_account_id,max(deletion_revision) FROM deletion_barriers GROUP BY creator_account_id;
CREATE TRIGGER derived_deletion_work_insert AFTER INSERT ON deletion_barriers
BEGIN
 INSERT INTO derived_deletion_work(creator_account_id,target_revision) VALUES(NEW.creator_account_id,NEW.deletion_revision)
 ON CONFLICT(creator_account_id) DO UPDATE SET target_revision=max(target_revision,excluded.target_revision);
END;
CREATE TRIGGER derived_deletion_work_update AFTER UPDATE OF deletion_revision ON deletion_barriers
BEGIN
 INSERT INTO derived_deletion_work(creator_account_id,target_revision) VALUES(NEW.creator_account_id,NEW.deletion_revision)
 ON CONFLICT(creator_account_id) DO UPDATE SET target_revision=max(target_revision,excluded.target_revision);
END;
CREATE TABLE derived_deletion_acknowledgements (
 operation_id TEXT PRIMARY KEY REFERENCES managed_deletion_operations(operation_id) ON DELETE CASCADE,
 creator_account_id TEXT NOT NULL, deletion_revision INTEGER NOT NULL,
 store_identity TEXT NOT NULL, receipt TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER derived_deletion_ack_immutable BEFORE UPDATE ON derived_deletion_acknowledgements
BEGIN SELECT RAISE(ABORT,'derived_deletion_ack_immutable'); END;
CREATE TRIGGER derived_deletion_ack_bound BEFORE INSERT ON derived_deletion_acknowledgements
WHEN NOT EXISTS(SELECT 1 FROM managed_deletion_operations WHERE operation_id=NEW.operation_id
 AND creator_account_id=NEW.creator_account_id AND deletion_revision=NEW.deletion_revision)
BEGIN SELECT RAISE(ABORT,'derived_deletion_ack_target_invalid'); END;
CREATE TRIGGER derived_deletion_ack_delete BEFORE DELETE ON derived_deletion_acknowledgements
WHEN EXISTS(SELECT 1 FROM managed_deletion_operations WHERE operation_id=OLD.operation_id)
BEGIN SELECT RAISE(ABORT,'derived_deletion_ack_referenced'); END;
UPDATE managed_deletion_operations SET status='pending' WHERE status='complete';
