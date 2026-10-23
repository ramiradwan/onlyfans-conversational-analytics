-- Non-text source metadata is frozen with future findings snapshots.
ALTER TABLE nlp_sources ADD COLUMN read_metadata TEXT NOT NULL DEFAULT '' CHECK(length(read_metadata)<=32768);
CREATE TABLE nlp_snapshot_source_pages (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 document BLOB NOT NULL CHECK(length(document)<=65536),
 erasure_seen_epoch INTEGER NOT NULL DEFAULT 0, erasure_invalid INTEGER NOT NULL DEFAULT 0 CHECK(erasure_invalid IN (0,1)),
 PRIMARY KEY(account_ref,snapshot_id,ordinal),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TRIGGER nlp_source_page_immutable BEFORE UPDATE OF account_ref,snapshot_id,ordinal,document ON nlp_snapshot_source_pages
BEGIN SELECT RAISE(ABORT,'nlp_source_page_immutable'); END;
CREATE TRIGGER nlp_source_page_delete BEFORE DELETE ON nlp_snapshot_source_pages
WHEN EXISTS(SELECT 1 FROM nlp_snapshots WHERE account_ref=OLD.account_ref AND snapshot_id=OLD.snapshot_id)
BEGIN SELECT RAISE(ABORT,'nlp_source_page_referenced'); END;
CREATE TRIGGER generation_content_nlp_source_page_insert AFTER INSERT ON nlp_snapshot_source_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_source_page_delete AFTER DELETE ON nlp_snapshot_source_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_source_page_update AFTER UPDATE ON nlp_snapshot_source_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
