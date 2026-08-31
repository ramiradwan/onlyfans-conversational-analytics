ALTER TABLE account_chats
    ADD COLUMN lifecycle_origin TEXT NOT NULL DEFAULT 'ordinary'
        CHECK (lifecycle_origin IN ('ordinary', 'creator_import'));
ALTER TABLE account_chats ADD COLUMN lifecycle_started_at TEXT;
UPDATE account_chats SET lifecycle_started_at=updated_at WHERE lifecycle_started_at IS NULL;

ALTER TABLE account_messages
    ADD COLUMN lifecycle_origin TEXT NOT NULL DEFAULT 'ordinary'
        CHECK (lifecycle_origin IN ('ordinary', 'creator_import'));
ALTER TABLE account_messages ADD COLUMN lifecycle_started_at TEXT;
UPDATE account_messages SET lifecycle_started_at=updated_at WHERE lifecycle_started_at IS NULL;

CREATE TABLE archive_policies (
    creator_account_id TEXT PRIMARY KEY,
    vault_enabled INTEGER NOT NULL DEFAULT 0 CHECK (vault_enabled IN (0, 1)),
    policy_type TEXT NOT NULL DEFAULT 'disabled' CHECK (
        policy_type IN ('disabled', 'finite', 'export_and_delete', 'indefinite_until_delete')
    ),
    finite_horizon_days INTEGER CHECK (finite_horizon_days IS NULL OR finite_horizon_days > 0),
    activated_at TEXT,
    creator_action_ref TEXT,
    policy_revision INTEGER NOT NULL DEFAULT 0 CHECK (policy_revision >= 0),
    indefinite_gate_state TEXT NOT NULL DEFAULT 'closed' CHECK (
        indefinite_gate_state IN ('closed', 'open')
    ),
    updated_at TEXT NOT NULL,
    CHECK (
        (policy_type='disabled' AND vault_enabled=0 AND finite_horizon_days IS NULL)
        OR (policy_type='finite' AND vault_enabled=1 AND finite_horizon_days IS NOT NULL)
        OR (policy_type='export_and_delete' AND vault_enabled=1 AND finite_horizon_days IS NULL)
        OR (
            policy_type='indefinite_until_delete' AND vault_enabled=1
            AND finite_horizon_days IS NULL AND indefinite_gate_state='open'
        )
    )
);

INSERT INTO archive_policies(creator_account_id, updated_at)
SELECT creator_account_id, updated_at FROM account_heads;

CREATE TABLE archive_membership (
    creator_account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source_event_at TEXT NOT NULL,
    working_purpose INTEGER NOT NULL DEFAULT 1 CHECK (working_purpose IN (0, 1)),
    vault_purpose INTEGER NOT NULL DEFAULT 0 CHECK (vault_purpose IN (0, 1)),
    vault_policy_revision INTEGER CHECK (
        vault_policy_revision IS NULL OR vault_policy_revision >= 0
    ),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, message_id),
    FOREIGN KEY (creator_account_id, message_id)
        REFERENCES account_messages (creator_account_id, message_id) ON DELETE CASCADE
);

INSERT INTO archive_membership(
    creator_account_id, message_id, source_event_at,
    working_purpose, vault_purpose, vault_policy_revision, updated_at
)
SELECT creator_account_id, message_id, sent_at, 1, 0, NULL, updated_at
FROM account_messages
WHERE is_deleted=0;

CREATE TABLE deletion_barriers (
    creator_account_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK (
        scope_kind IN ('account', 'conversation', 'message', 'participant')
    ),
    scope_key TEXT NOT NULL,
    deletion_revision INTEGER NOT NULL CHECK (deletion_revision > 0),
    deleted_at TEXT NOT NULL,
    provenance TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, scope_kind, scope_key)
);

CREATE INDEX deletion_barriers_account_revision
    ON deletion_barriers (creator_account_id, deletion_revision);

-- Participant deletion is participant-conversation scoped.
--
-- Keep only the participant-to-chat deletion identity needed to recognize
-- stale replay after the canonical chat row is gone. No deleted message text
-- or display metadata is retained here.
CREATE TABLE participant_deletion_chat_scopes (
    creator_account_id TEXT NOT NULL,
    participant_scope_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    deletion_revision INTEGER NOT NULL CHECK (deletion_revision > 0),
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, participant_scope_key, chat_id)
) WITHOUT ROWID;

CREATE INDEX participant_deletion_chat_scopes_account_chat
    ON participant_deletion_chat_scopes (
        creator_account_id,
        chat_id
    );

-- Seed participant deletion chat scopes from existing chats.
INSERT OR REPLACE INTO participant_deletion_chat_scopes(
    creator_account_id,participant_scope_key,chat_id,deletion_revision,deleted_at
)
SELECT b.creator_account_id,b.scope_key,c.chat_id,b.deletion_revision,b.deleted_at
  FROM deletion_barriers AS b
  JOIN account_chats AS c
    ON c.creator_account_id=b.creator_account_id
   AND c.platform_user_id=b.scope_key
 WHERE b.scope_kind='participant';

-- Creator-directed deletion must remove source-stream provenance that could
-- otherwise keep a deleted entity reconstructible even when canonical ingress
-- correctly suppresses stale replay.
DELETE FROM stream_message_membership
WHERE EXISTS (
    SELECT 1
      FROM deletion_barriers AS b
     WHERE b.creator_account_id=stream_message_membership.creator_account_id
       AND (
           b.scope_kind='account'
           OR (b.scope_kind='message' AND b.scope_key=stream_message_membership.message_id)
           OR (b.scope_kind='conversation' AND b.scope_key=stream_message_membership.chat_id)
           OR b.scope_kind='participant'
       )
);

DELETE FROM stream_chat_membership
WHERE EXISTS (
    SELECT 1
      FROM deletion_barriers AS b
     WHERE b.creator_account_id=stream_chat_membership.creator_account_id
       AND (
           b.scope_kind='account'
           OR (b.scope_kind='conversation' AND b.scope_key=stream_chat_membership.chat_id)
       )
);

-- SQLite UNIQUE constraints treat NULL values as distinct. projection_work
-- uses NULL conversation_id for account-wide reseeds, so INSERT OR IGNORE alone
-- does not prevent duplicate pending reseeds at the same canonical revision.
DELETE FROM projection_work
WHERE work_id IN (
    SELECT newer.work_id
      FROM projection_work AS newer
      JOIN projection_work AS older
        ON older.creator_account_id=newer.creator_account_id
       AND older.canonical_revision=newer.canonical_revision
       AND older.work_kind='reseed'
       AND newer.work_kind='reseed'
       AND older.conversation_id IS NULL
       AND newer.conversation_id IS NULL
       AND older.completed_at IS NULL
       AND newer.completed_at IS NULL
       AND older.work_id<newer.work_id
);

CREATE TRIGGER archive_policy_default_on_account
AFTER INSERT ON account_heads
BEGIN
    INSERT OR IGNORE INTO archive_policies(creator_account_id, updated_at)
    VALUES (NEW.creator_account_id, NEW.updated_at);
END;

CREATE TRIGGER archive_membership_on_message_insert
AFTER INSERT ON account_messages
WHEN NEW.is_deleted=0
BEGIN
    INSERT INTO archive_membership(
        creator_account_id, message_id, source_event_at,
        working_purpose, vault_purpose, vault_policy_revision, updated_at
    )
    VALUES (
        NEW.creator_account_id,
        NEW.message_id,
        NEW.sent_at,
        1,
        COALESCE((
            SELECT CASE
                WHEN vault_enabled=1 AND (
                    policy_type<>'indefinite_until_delete' OR indefinite_gate_state='open'
                ) THEN 1 ELSE 0 END
            FROM archive_policies WHERE creator_account_id=NEW.creator_account_id
        ), 0),
        (SELECT policy_revision FROM archive_policies
         WHERE creator_account_id=NEW.creator_account_id),
        NEW.updated_at
    )
    ON CONFLICT(creator_account_id, message_id) DO UPDATE SET
        source_event_at=excluded.source_event_at,
        working_purpose=1,
        vault_purpose=excluded.vault_purpose,
        vault_policy_revision=excluded.vault_policy_revision,
        updated_at=excluded.updated_at;
END;

CREATE TRIGGER deletion_barrier_scrub_raw_ingest_insert
AFTER INSERT ON deletion_barriers
BEGIN
    DELETE FROM raw_ingest_events
    WHERE creator_account_id=NEW.creator_account_id
      AND (
          NEW.scope_kind='account'
          OR (
              NEW.scope_kind='message'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key='message_id' AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
          OR (
              NEW.scope_kind='conversation'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key IN ('chat_id','conversation_id') AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
          OR (
              NEW.scope_kind='participant'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key IN ('sender_platform_user_id','platform_user_id')
                    AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
      );
END;

CREATE TRIGGER deletion_barrier_scrub_raw_ingest_update
AFTER UPDATE OF scope_kind,scope_key,deletion_revision,deleted_at ON deletion_barriers
BEGIN
    DELETE FROM raw_ingest_events
    WHERE creator_account_id=NEW.creator_account_id
      AND (
          NEW.scope_kind='account'
          OR (
              NEW.scope_kind='message'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key='message_id' AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
          OR (
              NEW.scope_kind='conversation'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key IN ('chat_id','conversation_id') AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
          OR (
              NEW.scope_kind='participant'
              AND EXISTS (
                  SELECT 1 FROM json_tree(raw_ingest_events.event_json)
                  WHERE key IN ('sender_platform_user_id','platform_user_id')
                    AND CAST(value AS TEXT)=NEW.scope_key
              )
          )
      );
END;

-- Stream membership is provenance, not source authority. Requiring an active
-- canonical row prevents barrier-suppressed replay from leaving a second
-- reconstruction index behind.
CREATE TRIGGER stream_chat_membership_requires_active_canonical
BEFORE INSERT ON stream_chat_membership
WHEN NOT EXISTS (
    SELECT 1 FROM account_chats AS c
    WHERE c.creator_account_id=NEW.creator_account_id
      AND c.chat_id=NEW.chat_id
      AND c.is_deleted=0
)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER stream_message_membership_requires_active_canonical
BEFORE INSERT ON stream_message_membership
WHEN NOT EXISTS (
    SELECT 1 FROM account_messages AS m
    WHERE m.creator_account_id=NEW.creator_account_id
      AND m.message_id=NEW.message_id
      AND m.chat_id=NEW.chat_id
      AND m.is_deleted=0
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Creator-directed deletion changes canonical truth even though it does not
-- arrive through protocol ingestion. Every barrier revision therefore receives
-- a fresh canonical revision, ensuring a completed projection reseed at the
-- previous revision cannot absorb the deletion-driven rebuild request.
CREATE TRIGGER deletion_barrier_advances_canonical_revision_insert
AFTER INSERT ON deletion_barriers
BEGIN
    UPDATE account_heads
       SET canonical_revision=canonical_revision+1,
           updated_at=NEW.deleted_at
     WHERE creator_account_id=NEW.creator_account_id;

    INSERT OR IGNORE INTO projection_work(
        creator_account_id,canonical_revision,work_kind,conversation_id,created_at
    )
    SELECT NEW.creator_account_id,canonical_revision,'reseed',NULL,NEW.deleted_at
      FROM account_heads
     WHERE creator_account_id=NEW.creator_account_id;
END;

CREATE TRIGGER deletion_barrier_advances_canonical_revision_update
AFTER UPDATE OF deletion_revision,deleted_at,provenance ON deletion_barriers
BEGIN
    UPDATE account_heads
       SET canonical_revision=canonical_revision+1,
           updated_at=NEW.deleted_at
     WHERE creator_account_id=NEW.creator_account_id;

    INSERT OR IGNORE INTO projection_work(
        creator_account_id,canonical_revision,work_kind,conversation_id,created_at
    )
    SELECT NEW.creator_account_id,canonical_revision,'reseed',NULL,NEW.deleted_at
      FROM account_heads
     WHERE creator_account_id=NEW.creator_account_id;
END;

CREATE TRIGGER projection_work_global_reseed_pending_dedupe
BEFORE INSERT ON projection_work
WHEN NEW.work_kind='reseed'
 AND NEW.conversation_id IS NULL
 AND EXISTS (
     SELECT 1
       FROM projection_work
      WHERE creator_account_id=NEW.creator_account_id
        AND canonical_revision=NEW.canonical_revision
        AND work_kind='reseed'
        AND conversation_id IS NULL
        AND completed_at IS NULL
 )
BEGIN
    SELECT RAISE(IGNORE);
END;

-- One mapped chat is a complete deletion unit: managed snapshot copies,
-- reconstruction indexes, every message direction, and the chat itself.
CREATE TRIGGER participant_deletion_chat_scope_scrub
AFTER INSERT ON participant_deletion_chat_scopes
BEGIN
    DELETE FROM committed_snapshots
     WHERE creator_account_id=NEW.creator_account_id
       AND EXISTS (
           SELECT 1 FROM snapshot_chat_records AS c
            WHERE c.creator_account_id=NEW.creator_account_id
              AND c.agent_installation_id=committed_snapshots.agent_installation_id
              AND c.agent_stream_id=committed_snapshots.agent_stream_id
              AND c.snapshot_id=committed_snapshots.snapshot_id
              AND c.chat_id=NEW.chat_id
       );

    DELETE FROM snapshot_uploads
     WHERE creator_account_id=NEW.creator_account_id
       AND EXISTS (
           SELECT 1 FROM snapshot_chat_records AS c
            WHERE c.creator_account_id=NEW.creator_account_id
              AND c.agent_installation_id=snapshot_uploads.agent_installation_id
              AND c.agent_stream_id=snapshot_uploads.agent_stream_id
              AND c.snapshot_id=snapshot_uploads.snapshot_id
              AND c.chat_id=NEW.chat_id
       );

    DELETE FROM raw_ingest_events
     WHERE creator_account_id=NEW.creator_account_id
       AND EXISTS (
           SELECT 1 FROM json_tree(raw_ingest_events.event_json)
            WHERE key IN ('chat_id','conversation_id')
              AND CAST(value AS TEXT)=NEW.chat_id
       );

    DELETE FROM stream_message_membership
     WHERE creator_account_id=NEW.creator_account_id AND chat_id=NEW.chat_id;

    DELETE FROM stream_chat_membership
     WHERE creator_account_id=NEW.creator_account_id AND chat_id=NEW.chat_id;

    DELETE FROM account_messages
     WHERE creator_account_id=NEW.creator_account_id AND chat_id=NEW.chat_id;

    DELETE FROM account_chats
     WHERE creator_account_id=NEW.creator_account_id AND chat_id=NEW.chat_id;
END;

-- Capture the chat identity before CreatorVaultRetention._purge_scope deletes
-- the remaining participant-authored rows. Repeated creator deletion after a
-- deliberate import refreshes the same mapping and scrubs that new lifecycle.
CREATE TRIGGER deletion_barrier_capture_participant_chats_insert
AFTER INSERT ON deletion_barriers
WHEN NEW.scope_kind='participant'
BEGIN
    INSERT OR REPLACE INTO participant_deletion_chat_scopes(
        creator_account_id,participant_scope_key,chat_id,deletion_revision,deleted_at
    )
    SELECT NEW.creator_account_id,NEW.scope_key,chat_id,NEW.deletion_revision,NEW.deleted_at
      FROM account_chats
     WHERE creator_account_id=NEW.creator_account_id
       AND platform_user_id=NEW.scope_key;
END;

CREATE TRIGGER deletion_barrier_capture_participant_chats_update
AFTER UPDATE OF scope_kind,scope_key,deletion_revision,deleted_at ON deletion_barriers
WHEN NEW.scope_kind='participant'
BEGIN
    INSERT OR REPLACE INTO participant_deletion_chat_scopes(
        creator_account_id,participant_scope_key,chat_id,deletion_revision,deleted_at
    )
    SELECT NEW.creator_account_id,NEW.scope_key,chat_id,NEW.deletion_revision,NEW.deleted_at
      FROM account_chats
     WHERE creator_account_id=NEW.creator_account_id
       AND platform_user_id=NEW.scope_key;
END;

-- Scrub stream-message and stream-chat membership for account, message, and
-- conversation deletion barriers. Participant chat provenance is scoped above.
CREATE TRIGGER deletion_barrier_scrub_stream_membership_insert
AFTER INSERT ON deletion_barriers
BEGIN
    DELETE FROM stream_message_membership
     WHERE creator_account_id=NEW.creator_account_id
       AND (
           NEW.scope_kind='account'
           OR (NEW.scope_kind='message' AND message_id=NEW.scope_key)
           OR (NEW.scope_kind='conversation' AND chat_id=NEW.scope_key)
       );
    DELETE FROM stream_chat_membership
     WHERE creator_account_id=NEW.creator_account_id
       AND (
           NEW.scope_kind='account'
           OR (NEW.scope_kind='conversation' AND chat_id=NEW.scope_key)
       );
END;

CREATE TRIGGER deletion_barrier_scrub_stream_membership_update
AFTER UPDATE OF scope_kind,scope_key,deletion_revision,deleted_at ON deletion_barriers
BEGIN
    DELETE FROM stream_message_membership
     WHERE creator_account_id=NEW.creator_account_id
       AND (
           NEW.scope_kind='account'
           OR (NEW.scope_kind='message' AND message_id=NEW.scope_key)
           OR (NEW.scope_kind='conversation' AND chat_id=NEW.scope_key)
       );
    DELETE FROM stream_chat_membership
     WHERE creator_account_id=NEW.creator_account_id
       AND (
           NEW.scope_kind='account'
           OR (NEW.scope_kind='conversation' AND chat_id=NEW.scope_key)
       );
END;

-- Ordinary canonical replay is fenced by both direct deletion barriers and the
-- durable participant-chat scope. Explicit creator_import remains a deliberate
-- new lifecycle.
CREATE TRIGGER deletion_barrier_guard_chat_insert
BEFORE INSERT ON account_chats
WHEN NEW.lifecycle_origin='ordinary' AND (
    EXISTS (
        SELECT 1 FROM deletion_barriers AS b
         WHERE b.creator_account_id=NEW.creator_account_id
           AND (
               (b.scope_kind='account' AND b.scope_key='*')
               OR (b.scope_kind='conversation' AND b.scope_key=NEW.chat_id)
               OR (b.scope_kind='participant' AND b.scope_key=NEW.platform_user_id)
           )
    )
    OR EXISTS (
        SELECT 1 FROM participant_deletion_chat_scopes AS s
         WHERE s.creator_account_id=NEW.creator_account_id
           AND s.chat_id=NEW.chat_id
    )
)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER deletion_barrier_guard_message_insert
BEFORE INSERT ON account_messages
WHEN NEW.lifecycle_origin='ordinary' AND (
    EXISTS (
        SELECT 1 FROM deletion_barriers AS b
         WHERE b.creator_account_id=NEW.creator_account_id
           AND (
               (b.scope_kind='account' AND b.scope_key='*')
               OR (b.scope_kind='conversation' AND b.scope_key=NEW.chat_id)
               OR (b.scope_kind='message' AND b.scope_key=NEW.message_id)
               OR (b.scope_kind='participant' AND b.scope_key=NEW.sender_platform_user_id)
           )
    )
    OR EXISTS (
        SELECT 1 FROM participant_deletion_chat_scopes AS s
         WHERE s.creator_account_id=NEW.creator_account_id
           AND s.chat_id=NEW.chat_id
    )
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Raw ingress can arrive after canonical deletion with only a chat identifier.
CREATE TRIGGER deletion_barrier_redact_raw_ingest_insert
AFTER INSERT ON raw_ingest_events
WHEN EXISTS (
    SELECT 1 FROM deletion_barriers AS b
     WHERE b.creator_account_id=NEW.creator_account_id
       AND (
           (b.scope_kind='account' AND b.scope_key='*')
           OR (
               b.scope_kind='message'
               AND EXISTS (
                   SELECT 1 FROM json_tree(NEW.event_json)
                    WHERE key='message_id' AND CAST(value AS TEXT)=b.scope_key
               )
           )
           OR (
               b.scope_kind='conversation'
               AND EXISTS (
                   SELECT 1 FROM json_tree(NEW.event_json)
                    WHERE key IN ('chat_id','conversation_id')
                      AND CAST(value AS TEXT)=b.scope_key
               )
           )
           OR (
               b.scope_kind='participant'
               AND EXISTS (
                   SELECT 1 FROM json_tree(NEW.event_json)
                    WHERE key IN ('sender_platform_user_id','platform_user_id')
                      AND CAST(value AS TEXT)=b.scope_key
               )
           )
       )
)
OR EXISTS (
    SELECT 1 FROM participant_deletion_chat_scopes AS s
     WHERE s.creator_account_id=NEW.creator_account_id
       AND EXISTS (
           SELECT 1 FROM json_tree(NEW.event_json)
            WHERE key IN ('chat_id','conversation_id')
              AND CAST(value AS TEXT)=s.chat_id
       )
)
BEGIN
    UPDATE raw_ingest_events
       SET event_json='{"redacted_by_deletion_barrier":true}'
     WHERE creator_account_id=NEW.creator_account_id
       AND agent_installation_id=NEW.agent_installation_id
       AND agent_stream_id=NEW.agent_stream_id
       AND event_id=NEW.event_id;
END;

CREATE TRIGGER deletion_barrier_guard_snapshot_chat_insert
BEFORE INSERT ON snapshot_chat_records
WHEN EXISTS (
    SELECT 1 FROM deletion_barriers AS b
     WHERE b.creator_account_id=NEW.creator_account_id
       AND (
           (b.scope_kind='account' AND b.scope_key='*')
           OR (b.scope_kind='conversation' AND b.scope_key=NEW.chat_id)
           OR (b.scope_kind='participant' AND b.scope_key=NEW.platform_user_id)
       )
)
OR EXISTS (
    SELECT 1 FROM participant_deletion_chat_scopes AS s
     WHERE s.creator_account_id=NEW.creator_account_id
       AND s.chat_id=NEW.chat_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER deletion_barrier_guard_snapshot_message_insert
BEFORE INSERT ON snapshot_message_records
WHEN EXISTS (
    SELECT 1 FROM deletion_barriers AS b
     WHERE b.creator_account_id=NEW.creator_account_id
       AND (
           (b.scope_kind='account' AND b.scope_key='*')
           OR (b.scope_kind='conversation' AND b.scope_key=NEW.chat_id)
           OR (b.scope_kind='message' AND b.scope_key=NEW.message_id)
           OR (b.scope_kind='participant' AND b.scope_key=NEW.sender_platform_user_id)
       )
)
OR EXISTS (
    SELECT 1 FROM participant_deletion_chat_scopes AS s
     WHERE s.creator_account_id=NEW.creator_account_id
       AND s.chat_id=NEW.chat_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;
