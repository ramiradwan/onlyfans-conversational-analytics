CREATE TABLE text_analysis_enrollment_capsules (
 enrollment_id TEXT PRIMARY KEY REFERENCES text_analysis_enrollments(enrollment_id),
 profile BLOB NOT NULL CHECK(length(profile) BETWEEN 1 AND 1048576),
 profile_bytes_sha256 TEXT NOT NULL,
 identity BLOB NOT NULL CHECK(length(identity) BETWEEN 1 AND 65536),
 identity_sha256 TEXT NOT NULL, capsule_sha256 TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE text_analysis_account_current (
 installation_id TEXT NOT NULL, creator_account_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 mode_version INTEGER NOT NULL CHECK(mode_version BETWEEN 0 AND 9007199254740991),
 enrollment_id TEXT REFERENCES text_analysis_enrollments(enrollment_id),
 PRIMARY KEY(installation_id,creator_account_id), UNIQUE(enrollment_id)
) WITHOUT ROWID;
INSERT INTO text_analysis_account_current
 SELECT installation_id,creator_account_id,principal_id,1,enrollment_id
 FROM text_analysis_enrollments WHERE state IN ('enabled','paused');
CREATE TABLE text_analysis_enrollment_commands (
 installation_id TEXT NOT NULL, command_key TEXT NOT NULL,
 creator_account_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 meaning BLOB NOT NULL CHECK(length(meaning) BETWEEN 1 AND 4096), meaning_sha256 TEXT NOT NULL,
 receipt BLOB NOT NULL CHECK(length(receipt) BETWEEN 1 AND 4096),
 PRIMARY KEY(installation_id,command_key)
) WITHOUT ROWID;
CREATE TRIGGER text_analysis_capsule_immutable BEFORE UPDATE ON text_analysis_enrollment_capsules
 BEGIN SELECT RAISE(ABORT,'text_analysis_capsule_immutable'); END;
CREATE TRIGGER text_analysis_command_immutable BEFORE UPDATE ON text_analysis_enrollment_commands
 BEGIN SELECT RAISE(ABORT,'text_analysis_command_immutable'); END;
CREATE TRIGGER text_analysis_capsule_retained BEFORE DELETE ON text_analysis_enrollment_capsules
 BEGIN SELECT RAISE(ABORT,'text_analysis_capsule_retained'); END;
CREATE TRIGGER text_analysis_command_retained BEFORE DELETE ON text_analysis_enrollment_commands
 BEGIN SELECT RAISE(ABORT,'text_analysis_command_retained'); END;
