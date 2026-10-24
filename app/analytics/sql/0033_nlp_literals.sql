-- Immutable source-derived literal material and snapshot-owned publications.
CREATE TABLE nlp_literal_accounts (
 account_ref TEXT PRIMARY KEY REFERENCES nlp_accounts(account_ref) ON DELETE CASCADE,
 namespace TEXT NOT NULL UNIQUE
) WITHOUT ROWID;
CREATE TABLE nlp_literal_derivations (
 account_ref TEXT NOT NULL, record_digest TEXT NOT NULL, document BLOB NOT NULL CHECK(length(document)<=65536),
 PRIMARY KEY(account_ref,record_digest),
 FOREIGN KEY(account_ref,record_digest) REFERENCES nlp_records(account_ref,record_digest) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE nlp_literal_derivation_batches (
 account_ref TEXT NOT NULL, record_digest TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 digest TEXT NOT NULL, document BLOB NOT NULL CHECK(length(document)<=65536),
 PRIMARY KEY(account_ref,record_digest,ordinal),
 FOREIGN KEY(account_ref,record_digest) REFERENCES nlp_literal_derivations(account_ref,record_digest) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE nlp_literal_publications (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, owner TEXT NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('building','sealed')), publication_id TEXT,
 document BLOB NOT NULL CHECK(length(document)<=65536),
 PRIMARY KEY(account_ref,snapshot_id),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE nlp_literal_publication_batches (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 digest TEXT NOT NULL, document BLOB NOT NULL CHECK(length(document)<=65536),
 PRIMARY KEY(account_ref,snapshot_id,ordinal),
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_literal_publications(account_ref,snapshot_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TRIGGER nlp_literal_accounts_immutable BEFORE UPDATE ON nlp_literal_accounts
BEGIN SELECT RAISE(ABORT,'nlp_literal_namespace_immutable'); END;
CREATE TRIGGER nlp_literal_derivations_immutable BEFORE UPDATE ON nlp_literal_derivations
BEGIN SELECT RAISE(ABORT,'nlp_literal_derivation_immutable'); END;
CREATE TRIGGER nlp_literal_derivation_batches_immutable BEFORE UPDATE ON nlp_literal_derivation_batches
BEGIN SELECT RAISE(ABORT,'nlp_literal_batch_immutable'); END;
CREATE TRIGGER nlp_literal_publications_immutable BEFORE UPDATE ON nlp_literal_publications
WHEN OLD.state!='building' OR NEW.state!='sealed' OR NEW.account_ref!=OLD.account_ref
 OR NEW.snapshot_id!=OLD.snapshot_id OR NEW.owner!=OLD.owner OR NEW.expires_at!=OLD.expires_at
BEGIN SELECT RAISE(ABORT,'nlp_literal_publication_immutable'); END;
CREATE TRIGGER nlp_literal_publication_batches_immutable BEFORE UPDATE ON nlp_literal_publication_batches
BEGIN SELECT RAISE(ABORT,'nlp_literal_batch_immutable'); END;
CREATE TRIGGER nlp_literal_publication_batches_building BEFORE INSERT ON nlp_literal_publication_batches
WHEN NOT EXISTS(SELECT 1 FROM nlp_literal_publications WHERE account_ref=NEW.account_ref
 AND snapshot_id=NEW.snapshot_id AND state='building')
BEGIN SELECT RAISE(ABORT,'nlp_literal_building_required'); END;
CREATE TRIGGER generation_content_nlp_literal_accounts_insert AFTER INSERT ON nlp_literal_accounts
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_accounts_update AFTER UPDATE ON nlp_literal_accounts
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_accounts_delete AFTER DELETE ON nlp_literal_accounts
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivations_insert AFTER INSERT ON nlp_literal_derivations
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivations_update AFTER UPDATE ON nlp_literal_derivations
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivations_delete AFTER DELETE ON nlp_literal_derivations
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivation_batches_insert AFTER INSERT ON nlp_literal_derivation_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivation_batches_update AFTER UPDATE ON nlp_literal_derivation_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_derivation_batches_delete AFTER DELETE ON nlp_literal_derivation_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publications_insert AFTER INSERT ON nlp_literal_publications
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publications_update AFTER UPDATE ON nlp_literal_publications
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publications_delete AFTER DELETE ON nlp_literal_publications
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publication_batches_insert AFTER INSERT ON nlp_literal_publication_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publication_batches_update AFTER UPDATE ON nlp_literal_publication_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_literal_publication_batches_delete AFTER DELETE ON nlp_literal_publication_batches
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER nlp_literal_accounts_replace_blocked BEFORE INSERT ON nlp_literal_accounts
WHEN EXISTS(SELECT 1 FROM nlp_literal_accounts WHERE account_ref=NEW.account_ref)
BEGIN SELECT RAISE(ABORT,'nlp_literal_replace_blocked'); END;
CREATE TRIGGER nlp_literal_derivations_replace_blocked BEFORE INSERT ON nlp_literal_derivations
WHEN EXISTS(SELECT 1 FROM nlp_literal_derivations WHERE account_ref=NEW.account_ref AND record_digest=NEW.record_digest)
BEGIN SELECT RAISE(ABORT,'nlp_literal_replace_blocked'); END;
CREATE TRIGGER nlp_literal_derivation_batches_replace_blocked BEFORE INSERT ON nlp_literal_derivation_batches
WHEN EXISTS(SELECT 1 FROM nlp_literal_derivation_batches WHERE account_ref=NEW.account_ref AND record_digest=NEW.record_digest AND ordinal=NEW.ordinal)
BEGIN SELECT RAISE(ABORT,'nlp_literal_replace_blocked'); END;
CREATE TRIGGER nlp_literal_publications_replace_blocked BEFORE INSERT ON nlp_literal_publications
WHEN EXISTS(SELECT 1 FROM nlp_literal_publications WHERE account_ref=NEW.account_ref AND snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'nlp_literal_replace_blocked'); END;
CREATE TRIGGER nlp_literal_publication_batches_replace_blocked BEFORE INSERT ON nlp_literal_publication_batches
WHEN EXISTS(SELECT 1 FROM nlp_literal_publication_batches WHERE account_ref=NEW.account_ref AND snapshot_id=NEW.snapshot_id AND ordinal=NEW.ordinal)
BEGIN SELECT RAISE(ABORT,'nlp_literal_replace_blocked'); END;
