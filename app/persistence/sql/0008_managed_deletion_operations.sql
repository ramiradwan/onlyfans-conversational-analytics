CREATE TABLE managed_deletion_operations (
    operation_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL,
    deletion_revision INTEGER NOT NULL CHECK (deletion_revision > 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'incomplete', 'complete')),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 64
    )
);

CREATE INDEX managed_deletion_operations_by_account_status
    ON managed_deletion_operations (creator_account_id, status, updated_at);

CREATE TRIGGER managed_account_deletion_operation_insert
AFTER INSERT ON deletion_barriers
WHEN NEW.scope_kind = 'account'
 AND (NEW.provenance GLOB 'creator_delete:*' OR NEW.provenance GLOB 'unlink_delete:*')
BEGIN
    INSERT OR IGNORE INTO managed_deletion_operations (
        operation_id, creator_account_id, deletion_revision, status,
        requested_at, updated_at
    ) VALUES (
        substr(NEW.provenance, instr(NEW.provenance, ':') + 1),
        NEW.creator_account_id,
        NEW.deletion_revision,
        'pending',
        NEW.deleted_at,
        NEW.deleted_at
    );
END;

CREATE TRIGGER managed_account_deletion_operation_update
AFTER UPDATE OF deletion_revision, deleted_at, provenance ON deletion_barriers
WHEN NEW.scope_kind = 'account'
 AND (NEW.provenance GLOB 'creator_delete:*' OR NEW.provenance GLOB 'unlink_delete:*')
BEGIN
    INSERT OR IGNORE INTO managed_deletion_operations (
        operation_id, creator_account_id, deletion_revision, status,
        requested_at, updated_at
    ) VALUES (
        substr(NEW.provenance, instr(NEW.provenance, ':') + 1),
        NEW.creator_account_id,
        NEW.deletion_revision,
        'pending',
        NEW.deleted_at,
        NEW.deleted_at
    );
END;
