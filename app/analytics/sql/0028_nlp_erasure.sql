-- account_wide holds zero or the exact account-deletion barrier revision.
CREATE TABLE nlp_erasure_state (
 account_ref TEXT PRIMARY KEY, target_revision INTEGER NOT NULL, account_wide INTEGER NOT NULL,
 phase TEXT NOT NULL, identity TEXT NOT NULL DEFAULT '', cursor TEXT NOT NULL DEFAULT '[]',
 scan_epoch INTEGER NOT NULL DEFAULT 0, record_cursor TEXT NOT NULL DEFAULT '',
 due_at TEXT, receipt TEXT, indexed_schema INTEGER NOT NULL DEFAULT 0,
 covered_account_revision INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE nlp_epoch_ledger (
 account_ref TEXT PRIMARY KEY, selection_epoch INTEGER NOT NULL
) WITHOUT ROWID;
INSERT INTO nlp_epoch_ledger SELECT account_ref,selection_epoch FROM nlp_accounts;
CREATE TABLE nlp_record_bindings (
 account_ref TEXT NOT NULL, record_digest TEXT NOT NULL, source_ref TEXT NOT NULL,
 source_version TEXT NOT NULL, speaker_ref TEXT NOT NULL, seen_epoch INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account_ref,record_digest),
 FOREIGN KEY(account_ref,record_digest) REFERENCES nlp_records(account_ref,record_digest) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX nlp_record_binding_source ON nlp_record_bindings(account_ref,source_ref,source_version,speaker_ref);
ALTER TABLE nlp_sources ADD COLUMN erasure_seen_epoch INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER nlp_record_binding_immutable BEFORE UPDATE OF account_ref,record_digest,source_ref,source_version,speaker_ref ON nlp_record_bindings
BEGIN SELECT RAISE(ABORT,'nlp_record_binding_immutable'); END;
CREATE TRIGGER nlp_record_binding_delete BEFORE DELETE ON nlp_record_bindings
WHEN EXISTS(SELECT 1 FROM nlp_records WHERE account_ref=OLD.account_ref AND record_digest=OLD.record_digest)
BEGIN SELECT RAISE(ABORT,'nlp_record_binding_referenced'); END;
CREATE TRIGGER nlp_epoch_monotonic BEFORE UPDATE ON nlp_epoch_ledger
WHEN NEW.account_ref!=OLD.account_ref OR NEW.selection_epoch<OLD.selection_epoch
BEGIN SELECT RAISE(ABORT,'nlp_epoch_regression'); END;
CREATE TRIGGER nlp_epoch_delete BEFORE DELETE ON nlp_epoch_ledger
BEGIN SELECT RAISE(ABORT,'nlp_epoch_delete_blocked'); END;
CREATE TRIGGER generation_content_nlp_binding_insert AFTER INSERT ON nlp_record_bindings
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_binding_update AFTER UPDATE ON nlp_record_bindings
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_binding_delete AFTER DELETE ON nlp_record_bindings
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_erasure_insert AFTER INSERT ON nlp_erasure_state
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_erasure_update AFTER UPDATE ON nlp_erasure_state
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_erasure_delete AFTER DELETE ON nlp_erasure_state
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
