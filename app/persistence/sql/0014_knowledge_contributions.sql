CREATE TABLE knowledge_store_identity (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL CHECK(length(store_id)=32)
);
INSERT INTO knowledge_store_identity VALUES(1,lower(hex(randomblob(16))));
CREATE TABLE knowledge_retirement_epochs (
 creator_account_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0)
) WITHOUT ROWID;
CREATE TABLE knowledge_jobs (
 job_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, request_id TEXT NOT NULL,
 contribution_id TEXT NOT NULL UNIQUE, creator_account_id TEXT NOT NULL,
 spec BLOB NOT NULL CHECK(length(spec)<=16384), spec_sha256 TEXT NOT NULL,
 profile BLOB NOT NULL CHECK(length(profile)<=1048576),
 state TEXT NOT NULL CHECK(state IN ('pending','running','committed')),
 attempt_fence INTEGER NOT NULL DEFAULT 0 CHECK(attempt_fence>=0),
 job_attempt_id TEXT, lease_expires_at REAL,
 created_at REAL NOT NULL, committed_at REAL,
 UNIQUE(installation_id,request_id), CHECK(job_id=request_id)
) WITHOUT ROWID;
CREATE TABLE knowledge_attempts (
 job_id TEXT NOT NULL REFERENCES knowledge_jobs(job_id) ON DELETE CASCADE,
 attempt_fence INTEGER NOT NULL CHECK(attempt_fence>0), job_attempt_id TEXT NOT NULL,
 claimed_at REAL NOT NULL,
 PRIMARY KEY(job_id,attempt_fence,job_attempt_id), UNIQUE(job_id,attempt_fence)
) WITHOUT ROWID;
CREATE TABLE knowledge_sources (
 job_id TEXT NOT NULL REFERENCES knowledge_jobs(job_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<100000),
 creator_account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 message_id TEXT NOT NULL, participant_id TEXT NOT NULL,
 source_ref TEXT NOT NULL, document BLOB NOT NULL CHECK(length(document)<=4096),
 PRIMARY KEY(job_id,ordinal), UNIQUE(job_id,source_ref)
) WITHOUT ROWID;
CREATE INDEX knowledge_sources_erasure ON knowledge_sources(creator_account_id,message_id);
CREATE INDEX knowledge_sources_participant ON knowledge_sources(creator_account_id,participant_id);
CREATE TABLE knowledge_members (
 job_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 document BLOB NOT NULL CHECK(length(document)<=65536),
 identity BLOB NOT NULL CHECK(length(identity)<=4096),
 producing_attempt_fence INTEGER NOT NULL, producing_job_attempt_id TEXT NOT NULL,
 PRIMARY KEY(job_id,ordinal),
 FOREIGN KEY(job_id,ordinal) REFERENCES knowledge_sources(job_id,ordinal) ON DELETE CASCADE,
 FOREIGN KEY(job_id,producing_attempt_fence,producing_job_attempt_id)
 REFERENCES knowledge_attempts(job_id,attempt_fence,job_attempt_id)
) WITHOUT ROWID;
CREATE TABLE knowledge_contributions (
 contribution_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES knowledge_jobs(job_id) ON DELETE CASCADE,
 manifest BLOB NOT NULL CHECK(length(manifest)<=16384), manifest_sha256 TEXT NOT NULL,
 sealing_attempt_fence INTEGER NOT NULL, sealing_job_attempt_id TEXT NOT NULL,
 source_valid_until REAL,
 FOREIGN KEY(job_id,sealing_attempt_fence,sealing_job_attempt_id)
 REFERENCES knowledge_attempts(job_id,attempt_fence,job_attempt_id)
) WITHOUT ROWID;
CREATE INDEX knowledge_member_digest ON knowledge_members(job_id,json_extract(identity,'$.record_sha256'));
CREATE TABLE knowledge_retired_intents (
 installation_id TEXT NOT NULL, request_id TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
 PRIMARY KEY(installation_id,request_id)
) WITHOUT ROWID;
CREATE TRIGGER knowledge_job_spec_immutable BEFORE UPDATE ON knowledge_jobs
WHEN NEW.spec!=OLD.spec OR NEW.spec_sha256!=OLD.spec_sha256 OR NEW.profile!=OLD.profile
 OR NEW.job_id!=OLD.job_id OR NEW.request_id!=OLD.request_id
 OR NEW.installation_id!=OLD.installation_id OR NEW.contribution_id!=OLD.contribution_id
 OR NEW.creator_account_id!=OLD.creator_account_id
BEGIN SELECT RAISE(ABORT,'knowledge_job_immutable'); END;
CREATE TRIGGER knowledge_job_terminal BEFORE UPDATE ON knowledge_jobs
WHEN OLD.state='committed' OR NEW.attempt_fence<OLD.attempt_fence
BEGIN SELECT RAISE(ABORT,'knowledge_job_terminal'); END;
CREATE TRIGGER knowledge_sources_immutable BEFORE UPDATE ON knowledge_sources
BEGIN SELECT RAISE(ABORT,'knowledge_source_immutable'); END;
CREATE TRIGGER knowledge_attempts_immutable BEFORE UPDATE ON knowledge_attempts
BEGIN SELECT RAISE(ABORT,'knowledge_attempt_immutable'); END;
CREATE TRIGGER knowledge_members_immutable BEFORE UPDATE ON knowledge_members
BEGIN SELECT RAISE(ABORT,'knowledge_member_immutable'); END;
CREATE TRIGGER knowledge_contributions_immutable BEFORE UPDATE ON knowledge_contributions
BEGIN SELECT RAISE(ABORT,'knowledge_contribution_immutable'); END;
CREATE TRIGGER knowledge_retired_immutable BEFORE UPDATE ON knowledge_retired_intents
BEGIN SELECT RAISE(ABORT,'knowledge_retired_immutable'); END;
CREATE TRIGGER knowledge_retire_intent BEFORE DELETE ON knowledge_jobs
BEGIN
 INSERT OR IGNORE INTO knowledge_retired_intents VALUES(OLD.installation_id,OLD.request_id,OLD.spec_sha256);
 INSERT INTO knowledge_retirement_epochs VALUES(OLD.creator_account_id,
 COALESCE((SELECT json_extract(document,'$.source.account_scope') FROM knowledge_sources WHERE job_id=OLD.job_id LIMIT 1),''),1)
 ON CONFLICT(creator_account_id) DO UPDATE SET epoch=epoch+1,
 account_ref=CASE WHEN excluded.account_ref!='' THEN excluded.account_ref ELSE account_ref END;
END;
CREATE TRIGGER knowledge_source_delete BEFORE DELETE ON account_messages
BEGIN
 DELETE FROM knowledge_jobs WHERE job_id IN (
  SELECT job_id FROM knowledge_sources WHERE creator_account_id=OLD.creator_account_id AND message_id=OLD.message_id);
END;
CREATE TRIGGER knowledge_source_change BEFORE UPDATE ON account_messages
WHEN NEW.text IS NOT OLD.text OR NEW.sent_at IS NOT OLD.sent_at OR NEW.direction IS NOT OLD.direction
 OR NEW.creator_account_id IS NOT OLD.creator_account_id OR NEW.message_id IS NOT OLD.message_id
 OR NEW.sender_platform_user_id IS NOT OLD.sender_platform_user_id
 OR NEW.chat_id IS NOT OLD.chat_id OR NEW.is_deleted IS NOT OLD.is_deleted
 OR NEW.upstream_updated_at IS NOT OLD.upstream_updated_at OR NEW.content_hash IS NOT OLD.content_hash
 OR NEW.winning_stream_epoch IS NOT OLD.winning_stream_epoch OR NEW.winning_source_seq IS NOT OLD.winning_source_seq
 OR NEW.lifecycle_origin IS NOT OLD.lifecycle_origin OR NEW.lifecycle_started_at IS NOT OLD.lifecycle_started_at
BEGIN
 DELETE FROM knowledge_jobs WHERE job_id IN (
  SELECT job_id FROM knowledge_sources WHERE creator_account_id=OLD.creator_account_id AND message_id=OLD.message_id);
END;
CREATE TRIGGER knowledge_chat_delete BEFORE DELETE ON account_chats
BEGIN
 DELETE FROM knowledge_jobs WHERE job_id IN (
  SELECT job_id FROM knowledge_sources WHERE creator_account_id=OLD.creator_account_id AND conversation_id=OLD.chat_id);
END;
CREATE TRIGGER knowledge_chat_change BEFORE UPDATE ON account_chats
WHEN NEW.is_deleted IS NOT OLD.is_deleted OR NEW.platform_user_id IS NOT OLD.platform_user_id
BEGIN
 DELETE FROM knowledge_jobs WHERE job_id IN (
  SELECT job_id FROM knowledge_sources WHERE creator_account_id=OLD.creator_account_id AND conversation_id=OLD.chat_id);
END;
CREATE TRIGGER knowledge_member_delete BEFORE DELETE ON knowledge_members
WHEN EXISTS(SELECT 1 FROM knowledge_contributions WHERE job_id=OLD.job_id)
 AND EXISTS(SELECT 1 FROM knowledge_jobs WHERE job_id=OLD.job_id)
BEGIN SELECT RAISE(ABORT,'knowledge_member_sealed'); END;
