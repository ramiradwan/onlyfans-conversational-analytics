CREATE TABLE text_analysis_preview_requests (
 job_id TEXT PRIMARY KEY REFERENCES text_analysis_intents(job_id),
 request BLOB NOT NULL CHECK(length(request)<=1024),
 request_sha256 TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE text_analysis_preview_capsules (
 job_id TEXT PRIMARY KEY REFERENCES knowledge_jobs(job_id) ON DELETE CASCADE,
 document BLOB NOT NULL CHECK(length(document)<=131072),
 document_sha256 TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER text_analysis_preview_request_immutable BEFORE UPDATE ON text_analysis_preview_requests
BEGIN SELECT RAISE(ABORT,'text_analysis_preview_request_immutable'); END;
CREATE TRIGGER text_analysis_preview_request_receipt BEFORE DELETE ON text_analysis_preview_requests
BEGIN SELECT RAISE(ABORT,'text_analysis_lifetime_receipt'); END;
CREATE TRIGGER text_analysis_preview_capsule_immutable BEFORE UPDATE ON text_analysis_preview_capsules
BEGIN SELECT RAISE(ABORT,'text_analysis_preview_capsule_immutable'); END;
