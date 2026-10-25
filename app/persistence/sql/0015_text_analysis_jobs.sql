-- Preserve existing canonical result bytes while extending execution state.
CREATE TABLE migration15_knowledge_jobs AS SELECT * FROM knowledge_jobs;
CREATE TABLE migration15_knowledge_attempts AS SELECT * FROM knowledge_attempts;
CREATE TABLE migration15_knowledge_sources AS SELECT * FROM knowledge_sources;
CREATE TABLE migration15_knowledge_members AS SELECT * FROM knowledge_members;
CREATE TABLE migration15_knowledge_contributions AS SELECT * FROM knowledge_contributions;
DROP TRIGGER knowledge_job_spec_immutable;
DROP TRIGGER knowledge_job_terminal;
DROP TRIGGER knowledge_sources_immutable;
DROP TRIGGER knowledge_attempts_immutable;
DROP TRIGGER knowledge_members_immutable;
DROP TRIGGER knowledge_contributions_immutable;
DROP TRIGGER knowledge_retired_immutable;
DROP TRIGGER knowledge_retire_intent;
DROP TRIGGER knowledge_source_delete;
DROP TRIGGER knowledge_source_change;
DROP TRIGGER knowledge_chat_delete;
DROP TRIGGER knowledge_chat_change;
DROP TRIGGER knowledge_member_delete;
DROP TABLE knowledge_contributions;
DROP TABLE knowledge_members;
DROP TABLE knowledge_sources;
DROP TABLE knowledge_attempts;
DROP TABLE knowledge_jobs;
CREATE TABLE knowledge_jobs (
 job_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, request_id TEXT NOT NULL,
 contribution_id TEXT NOT NULL UNIQUE, creator_account_id TEXT NOT NULL,
 spec BLOB NOT NULL CHECK(length(spec)<=16384), spec_sha256 TEXT NOT NULL,
 profile BLOB NOT NULL CHECK(length(profile)<=1048576),
 state TEXT NOT NULL CHECK(state IN ('prepared','pending','running','committed','failed')),
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
INSERT INTO knowledge_jobs SELECT * FROM migration15_knowledge_jobs;
DROP TABLE migration15_knowledge_jobs;
INSERT INTO knowledge_attempts SELECT * FROM migration15_knowledge_attempts;
DROP TABLE migration15_knowledge_attempts;
INSERT INTO knowledge_sources SELECT * FROM migration15_knowledge_sources;
DROP TABLE migration15_knowledge_sources;
INSERT INTO knowledge_members SELECT * FROM migration15_knowledge_members;
DROP TABLE migration15_knowledge_members;
INSERT INTO knowledge_contributions SELECT * FROM migration15_knowledge_contributions;
DROP TABLE migration15_knowledge_contributions;
CREATE TRIGGER knowledge_job_spec_immutable BEFORE UPDATE ON knowledge_jobs
WHEN NEW.spec!=OLD.spec OR NEW.spec_sha256!=OLD.spec_sha256 OR NEW.profile!=OLD.profile
 OR NEW.job_id!=OLD.job_id OR NEW.request_id!=OLD.request_id
 OR NEW.installation_id!=OLD.installation_id OR NEW.contribution_id!=OLD.contribution_id
 OR NEW.creator_account_id!=OLD.creator_account_id
BEGIN SELECT RAISE(ABORT,'knowledge_job_immutable'); END;
CREATE TRIGGER knowledge_job_terminal BEFORE UPDATE ON knowledge_jobs
WHEN OLD.state IN ('committed','failed') OR NEW.attempt_fence<OLD.attempt_fence
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

CREATE TRIGGER knowledge_job_state_transition BEFORE UPDATE OF state ON knowledge_jobs
WHEN (OLD.state='prepared' AND NEW.state NOT IN ('prepared','pending','failed'))
 OR (OLD.state='pending' AND NEW.state NOT IN ('pending','running','failed'))
 OR (OLD.state='running' AND NEW.state NOT IN ('running','pending','committed','failed'))
BEGIN SELECT RAISE(ABORT,'knowledge_job_state_invalid'); END;

CREATE TABLE text_analysis_enrollments (
 enrollment_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, external_key TEXT NOT NULL,
 creator_account_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 meaning BLOB NOT NULL CHECK(length(meaning)<=32768), meaning_sha256 TEXT NOT NULL,
 context BLOB NOT NULL CHECK(length(context)<=16384),
 state TEXT NOT NULL CHECK(state IN ('enabled','paused','retired')),
 created_at REAL NOT NULL, UNIQUE(installation_id,external_key)
) WITHOUT ROWID;
CREATE UNIQUE INDEX text_analysis_active_enrollment ON text_analysis_enrollments(installation_id,creator_account_id)
 WHERE state IN ('enabled','paused');
CREATE TABLE text_analysis_intents (
 job_id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL UNIQUE,
 installation_id TEXT NOT NULL, external_key TEXT,
 enrollment_id TEXT REFERENCES text_analysis_enrollments(enrollment_id), slot_sha256 TEXT UNIQUE,
 creator_account_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 authority_kind TEXT NOT NULL CHECK(authority_kind IN ('license','preview')),
 reference_id TEXT NOT NULL, compact_jws_sha256 TEXT NOT NULL,
 context BLOB NOT NULL CHECK(length(context)<=16384), meaning_sha256 TEXT NOT NULL,
 spec_sha256 TEXT NOT NULL, scope_sha256 TEXT NOT NULL, parameter_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','pending','running','committed','failed','result_deleted')),
 confirmation BLOB CHECK(length(confirmation)<=16384), confirmed_sha256 TEXT,
 preview_selection_policy BLOB CHECK(length(preview_selection_policy)<=4096),
 terminal_reason TEXT, created_at REAL NOT NULL,
 UNIQUE(installation_id,external_key),
 CHECK((enrollment_id IS NULL AND slot_sha256 IS NULL AND external_key IS NOT NULL)
    OR (enrollment_id IS NOT NULL AND slot_sha256 IS NOT NULL AND external_key IS NULL)),
 CHECK((authority_kind='preview' AND preview_selection_policy IS NOT NULL)
    OR (authority_kind='license' AND preview_selection_policy IS NULL))
) WITHOUT ROWID;
CREATE INDEX text_analysis_intents_account ON text_analysis_intents(creator_account_id);
CREATE TABLE text_analysis_preview_admissions (
 installation_id TEXT NOT NULL, permit_id TEXT NOT NULL,
 reference_id TEXT NOT NULL UNIQUE, compact_jws_sha256 TEXT NOT NULL, issuance_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('available','reserved','spent')),
 job_id TEXT, contribution_id TEXT,
 PRIMARY KEY(installation_id,permit_id),
 CHECK((state='available' AND job_id IS NULL AND contribution_id IS NULL)
    OR (state='reserved' AND job_id IS NOT NULL AND contribution_id IS NULL)
    OR (state='spent' AND job_id IS NOT NULL AND contribution_id IS NOT NULL))
) WITHOUT ROWID;
CREATE TABLE text_analysis_preview_outputs (
 contribution_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES knowledge_jobs(job_id) ON DELETE CASCADE,
 document BLOB NOT NULL CHECK(length(document)<=1048576), document_sha256 TEXT NOT NULL,
 sealing_attempt_fence INTEGER NOT NULL, sealing_job_attempt_id TEXT NOT NULL,
 FOREIGN KEY(job_id,sealing_attempt_fence,sealing_job_attempt_id)
 REFERENCES knowledge_attempts(job_id,attempt_fence,job_attempt_id)
) WITHOUT ROWID;
CREATE TRIGGER text_analysis_enrollment_immutable BEFORE UPDATE ON text_analysis_enrollments
WHEN NEW.enrollment_id!=OLD.enrollment_id OR NEW.installation_id!=OLD.installation_id
 OR NEW.external_key!=OLD.external_key OR NEW.creator_account_id!=OLD.creator_account_id
 OR NEW.principal_id!=OLD.principal_id OR NEW.meaning!=OLD.meaning
 OR NEW.meaning_sha256!=OLD.meaning_sha256 OR NEW.context!=OLD.context OR OLD.state='retired'
BEGIN SELECT RAISE(ABORT,'text_analysis_enrollment_immutable'); END;
CREATE TRIGGER text_analysis_enrollment_receipt BEFORE DELETE ON text_analysis_enrollments
BEGIN SELECT RAISE(ABORT,'text_analysis_lifetime_receipt'); END;
CREATE TRIGGER text_analysis_intent_immutable BEFORE UPDATE ON text_analysis_intents
WHEN NEW.job_id!=OLD.job_id OR NEW.contribution_id!=OLD.contribution_id
 OR NEW.installation_id!=OLD.installation_id OR NEW.external_key IS NOT OLD.external_key
 OR NEW.enrollment_id IS NOT OLD.enrollment_id OR NEW.slot_sha256 IS NOT OLD.slot_sha256
 OR NEW.creator_account_id!=OLD.creator_account_id OR NEW.principal_id!=OLD.principal_id
 OR NEW.authority_kind!=OLD.authority_kind OR NEW.reference_id!=OLD.reference_id
 OR NEW.compact_jws_sha256!=OLD.compact_jws_sha256 OR NEW.context!=OLD.context
 OR NEW.meaning_sha256!=OLD.meaning_sha256 OR NEW.spec_sha256!=OLD.spec_sha256
 OR NEW.scope_sha256!=OLD.scope_sha256 OR NEW.parameter_sha256!=OLD.parameter_sha256
 OR NEW.preview_selection_policy IS NOT OLD.preview_selection_policy
 OR (OLD.confirmed_sha256 IS NOT NULL AND NEW.confirmed_sha256 IS NOT OLD.confirmed_sha256)
 OR (OLD.state='result_deleted')
BEGIN SELECT RAISE(ABORT,'text_analysis_intent_immutable'); END;
CREATE TRIGGER text_analysis_intent_receipt BEFORE DELETE ON text_analysis_intents
BEGIN SELECT RAISE(ABORT,'text_analysis_lifetime_receipt'); END;
CREATE TRIGGER text_analysis_job_state AFTER UPDATE OF state ON knowledge_jobs
BEGIN UPDATE text_analysis_intents SET state=NEW.state WHERE job_id=NEW.job_id; END;
CREATE TRIGGER text_analysis_job_retire BEFORE DELETE ON knowledge_jobs
BEGIN
 UPDATE text_analysis_intents SET state='result_deleted',confirmation=NULL,terminal_reason='source_retired'
 WHERE job_id=OLD.job_id;
END;
CREATE TRIGGER text_analysis_preview_immutable BEFORE UPDATE ON text_analysis_preview_outputs
BEGIN SELECT RAISE(ABORT,'text_analysis_preview_immutable'); END;
CREATE TRIGGER text_analysis_admission_immutable BEFORE UPDATE ON text_analysis_preview_admissions
WHEN NEW.installation_id!=OLD.installation_id OR NEW.permit_id!=OLD.permit_id
 OR NEW.reference_id!=OLD.reference_id OR NEW.compact_jws_sha256!=OLD.compact_jws_sha256
 OR NEW.issuance_id!=OLD.issuance_id OR OLD.state='spent'
BEGIN SELECT RAISE(ABORT,'text_analysis_admission_immutable'); END;
CREATE TRIGGER text_analysis_admission_receipt BEFORE DELETE ON text_analysis_preview_admissions
BEGIN SELECT RAISE(ABORT,'text_analysis_lifetime_receipt'); END;
CREATE TRIGGER text_analysis_admission_transition BEFORE UPDATE ON text_analysis_preview_admissions
WHEN (OLD.state='available' AND NEW.state NOT IN ('available','reserved'))
 OR (OLD.state='reserved' AND NEW.state='reserved' AND NEW.job_id IS NOT OLD.job_id)
 OR (OLD.state='reserved' AND NEW.state='spent' AND NEW.job_id IS NOT OLD.job_id)
BEGIN SELECT RAISE(ABORT,'text_analysis_admission_conflict'); END;
