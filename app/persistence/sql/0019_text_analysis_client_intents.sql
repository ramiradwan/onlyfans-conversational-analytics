CREATE TABLE text_analysis_client_intents (
 installation_id TEXT NOT NULL, organization_id TEXT NOT NULL,
 creator_account_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('enrollment_command','preview_prepare','preview_confirm')),
 intent_key TEXT NOT NULL, body BLOB NOT NULL CHECK(length(body)>0 AND length(body)<=8192),
 body_sha256 TEXT NOT NULL, retained_at TEXT NOT NULL,
 PRIMARY KEY(installation_id,kind,intent_key)
) WITHOUT ROWID;
CREATE INDEX text_analysis_client_actor ON text_analysis_client_intents
 (installation_id,organization_id,creator_account_id,principal_id,kind,intent_key);
CREATE TRIGGER text_analysis_client_immutable BEFORE UPDATE ON text_analysis_client_intents
BEGIN SELECT RAISE(ABORT,'text_analysis_client_immutable'); END;
CREATE TRIGGER text_analysis_client_lifetime BEFORE DELETE ON text_analysis_client_intents
BEGIN SELECT RAISE(ABORT,'text_analysis_lifetime_receipt'); END;
