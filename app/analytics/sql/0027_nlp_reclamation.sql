CREATE TABLE nlp_snapshot_members (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 record_digest TEXT NOT NULL,
 PRIMARY KEY(account_ref,snapshot_id,ordinal),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id) ON DELETE CASCADE,
 FOREIGN KEY(account_ref,record_digest) REFERENCES nlp_records(account_ref,record_digest)
) WITHOUT ROWID;
CREATE INDEX nlp_snapshot_members_record ON nlp_snapshot_members(account_ref,record_digest);
CREATE INDEX nlp_generation_refs_snapshot ON nlp_generation_refs(account_ref,snapshot_id);
CREATE INDEX nlp_jobs_record ON nlp_jobs(account_ref,record_digest) WHERE state='done';
CREATE TABLE nlp_reclamation_cursor (
 account_ref TEXT PRIMARY KEY, record_cursor TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE TABLE nlp_snapshot_expirations (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 PRIMARY KEY(account_ref,snapshot_id),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE nlp_snapshot_inventory (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, next_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(next_ordinal>=0),
 complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1,2)),
 retired_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(retired_ordinal>=0),
 PRIMARY KEY(account_ref,snapshot_id),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TRIGGER nlp_members_immutable BEFORE UPDATE ON nlp_snapshot_members
BEGIN SELECT RAISE(ABORT,'nlp_members_immutable'); END;
CREATE TRIGGER nlp_members_delete BEFORE DELETE ON nlp_snapshot_members
WHEN EXISTS(SELECT 1 FROM nlp_snapshots WHERE account_ref=OLD.account_ref AND snapshot_id=OLD.snapshot_id)
AND NOT EXISTS(SELECT 1 FROM nlp_snapshot_inventory WHERE account_ref=OLD.account_ref AND snapshot_id=OLD.snapshot_id AND complete=2 AND retired_ordinal>OLD.ordinal)
BEGIN SELECT RAISE(ABORT,'nlp_members_referenced'); END;
CREATE TRIGGER nlp_members_insert BEFORE INSERT ON nlp_snapshot_members
WHEN EXISTS(SELECT 1 FROM nlp_snapshot_inventory WHERE account_ref=NEW.account_ref AND snapshot_id=NEW.snapshot_id AND complete!=0)
BEGIN SELECT RAISE(ABORT,'nlp_members_sealed'); END;
CREATE TRIGGER nlp_inventory_immutable BEFORE UPDATE ON nlp_snapshot_inventory
WHEN NEW.account_ref!=OLD.account_ref OR NEW.snapshot_id!=OLD.snapshot_id OR NEW.next_ordinal<OLD.next_ordinal
OR NEW.retired_ordinal<OLD.retired_ordinal OR NEW.retired_ordinal>NEW.next_ordinal
OR (OLD.complete=1 AND (NEW.complete!=2 OR NEW.next_ordinal!=OLD.next_ordinal))
OR (OLD.complete=2 AND (NEW.complete!=2 OR NEW.next_ordinal!=OLD.next_ordinal))
BEGIN SELECT RAISE(ABORT,'nlp_inventory_immutable'); END;
CREATE TRIGGER nlp_inventory_delete BEFORE DELETE ON nlp_snapshot_inventory
WHEN EXISTS(SELECT 1 FROM nlp_snapshots WHERE account_ref=OLD.account_ref AND snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'nlp_inventory_referenced'); END;
CREATE TRIGGER nlp_refs_retiring BEFORE INSERT ON nlp_generation_refs
WHEN EXISTS(SELECT 1 FROM nlp_snapshot_inventory WHERE account_ref=NEW.account_ref AND snapshot_id=NEW.snapshot_id AND complete=2)
BEGIN SELECT RAISE(ABORT,'nlp_snapshot_retiring'); END;
CREATE TRIGGER generation_content_nlp_members_insert AFTER INSERT ON nlp_snapshot_members
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_members_delete AFTER DELETE ON nlp_snapshot_members
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_inventory_insert AFTER INSERT ON nlp_snapshot_inventory
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_inventory_update AFTER UPDATE ON nlp_snapshot_inventory
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_inventory_delete AFTER DELETE ON nlp_snapshot_inventory
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_expiration_insert AFTER INSERT ON nlp_snapshot_expirations
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_expiration_delete AFTER DELETE ON nlp_snapshot_expirations
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
