-- Optional findings use independent records and immutable generation bindings.
CREATE TABLE nlp_accounts (
 account_ref TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
 selection_epoch INTEGER NOT NULL, completion_revision INTEGER NOT NULL DEFAULT 0,
 availability_revision INTEGER NOT NULL DEFAULT 0, admission_epoch INTEGER NOT NULL DEFAULT 0,
 mode TEXT NOT NULL CHECK(mode IN ('enabled','paused','withdrawn','removed')),
 inventory_digest TEXT NOT NULL DEFAULT '', notification_revision INTEGER NOT NULL DEFAULT 1,
 acknowledged_revision INTEGER NOT NULL DEFAULT 0, live_claims INTEGER NOT NULL DEFAULT 0,
 scan_identity TEXT NOT NULL DEFAULT '', scan_cursor TEXT NOT NULL DEFAULT '[]',
 scan_complete INTEGER NOT NULL DEFAULT 0, scan_epoch INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE nlp_sources (
 account_ref TEXT NOT NULL REFERENCES nlp_accounts(account_ref) ON DELETE CASCADE,
 source_ref TEXT NOT NULL, source_version TEXT NOT NULL, speaker_ref TEXT NOT NULL,
 conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_epoch INTEGER NOT NULL DEFAULT 0, lane TEXT NOT NULL CHECK(lane IN ('live','backfill')),
 PRIMARY KEY(account_ref,source_ref)
) WITHOUT ROWID;
CREATE TABLE nlp_jobs (
 account_ref TEXT NOT NULL REFERENCES nlp_accounts(account_ref) ON DELETE CASCADE,
 source_ref TEXT NOT NULL, source_version TEXT NOT NULL, selection_epoch INTEGER NOT NULL,
 admission_epoch INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','done','abandoned')),
 lane TEXT NOT NULL, owner TEXT, expires_at REAL, recoveries INTEGER NOT NULL DEFAULT 0,
 record_digest TEXT, terminal_reason TEXT NOT NULL DEFAULT '' CHECK(terminal_reason IN ('','unsupported_input','interrupted','record_limit')),
 PRIMARY KEY(account_ref,source_ref)
) WITHOUT ROWID;
CREATE UNIQUE INDEX nlp_one_running_job ON nlp_jobs((1)) WHERE state='running';
CREATE TABLE nlp_records (
 account_ref TEXT NOT NULL, record_digest TEXT NOT NULL,
 document BLOB NOT NULL CHECK(length(document)<=65536),
 PRIMARY KEY(account_ref,record_digest)
) WITHOUT ROWID;
CREATE TABLE nlp_snapshots (
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, document BLOB NOT NULL CHECK(length(document)<=8388608),
 PRIMARY KEY(account_ref,snapshot_id)
) WITHOUT ROWID;
CREATE TABLE nlp_generation_refs (
 generation_id TEXT PRIMARY KEY REFERENCES projection_generations(generation_id) ON DELETE CASCADE,
 account_ref TEXT NOT NULL, snapshot_id TEXT NOT NULL, base_config TEXT NOT NULL,
 FOREIGN KEY(account_ref,snapshot_id) REFERENCES nlp_snapshots(account_ref,snapshot_id)
) WITHOUT ROWID;
CREATE TRIGGER nlp_records_immutable BEFORE UPDATE ON nlp_records BEGIN SELECT RAISE(ABORT,'nlp_record_immutable'); END;
CREATE TRIGGER nlp_snapshots_immutable BEFORE UPDATE ON nlp_snapshots BEGIN SELECT RAISE(ABORT,'nlp_snapshot_immutable'); END;
CREATE TRIGGER nlp_generation_refs_immutable BEFORE UPDATE ON nlp_generation_refs BEGIN SELECT RAISE(ABORT,'nlp_binding_immutable'); END;
CREATE TRIGGER nlp_generation_refs_building BEFORE INSERT ON nlp_generation_refs
WHEN NOT EXISTS(SELECT 1 FROM projection_generations WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.account_ref AND status='building')
BEGIN SELECT RAISE(ABORT,'nlp_building_generation_required'); END;
CREATE TRIGGER generation_content_nlp_records_insert AFTER INSERT ON nlp_records BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_records_delete AFTER DELETE ON nlp_records BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_snapshots_insert AFTER INSERT ON nlp_snapshots BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_snapshots_delete AFTER DELETE ON nlp_snapshots BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_refs_insert AFTER INSERT ON nlp_generation_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_nlp_refs_delete AFTER DELETE ON nlp_generation_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
