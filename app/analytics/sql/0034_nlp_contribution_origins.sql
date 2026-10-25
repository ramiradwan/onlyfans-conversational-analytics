CREATE TABLE nlp_record_origins (
 account_ref TEXT NOT NULL, record_digest TEXT NOT NULL,
 canonical_store_id TEXT NOT NULL, contribution_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 PRIMARY KEY(account_ref,record_digest,canonical_store_id,contribution_id),
 FOREIGN KEY(account_ref,record_digest) REFERENCES nlp_records(account_ref,record_digest) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE nlp_origin_epochs (
 account_ref TEXT NOT NULL REFERENCES nlp_accounts(account_ref) ON DELETE CASCADE,
 canonical_store_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>=0),
 PRIMARY KEY(account_ref,canonical_store_id)
) WITHOUT ROWID;
CREATE TRIGGER nlp_record_origins_immutable BEFORE UPDATE ON nlp_record_origins
BEGIN SELECT RAISE(ABORT,'nlp_origin_immutable'); END;
CREATE TRIGGER nlp_record_origins_delete BEFORE DELETE ON nlp_record_origins
WHEN EXISTS(SELECT 1 FROM nlp_records WHERE account_ref=OLD.account_ref AND record_digest=OLD.record_digest)
BEGIN SELECT RAISE(ABORT,'nlp_origin_referenced'); END;
CREATE TRIGGER generation_content_nlp_origins_insert AFTER INSERT ON nlp_record_origins
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_origins_delete AFTER DELETE ON nlp_record_origins
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_origin_epoch_insert AFTER INSERT ON nlp_origin_epochs
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_origin_epoch_update AFTER UPDATE ON nlp_origin_epochs
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_origin_epoch_delete AFTER DELETE ON nlp_origin_epochs
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
