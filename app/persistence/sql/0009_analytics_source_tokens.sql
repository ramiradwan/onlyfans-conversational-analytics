-- Invalidation metadata changes in the same transaction as source records.
CREATE TABLE analytics_source_tokens (
    creator_account_id TEXT PRIMARY KEY,
    token TEXT NOT NULL CHECK (length(token)=32 AND token NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;
INSERT INTO analytics_source_tokens SELECT creator_account_id,lower(hex(randomblob(16))) FROM account_heads;
CREATE INDEX analytics_message_time_lookup
    ON account_messages(creator_account_id,chat_id,is_deleted,julianday(sent_at) DESC);

CREATE TRIGGER analytics_source_token_account_heads_insert AFTER INSERT ON account_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_heads_update AFTER UPDATE OF canonical_revision,creator_account_id ON account_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_heads_delete AFTER DELETE ON account_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_chats_insert AFTER INSERT ON account_chats
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_chats_update AFTER UPDATE ON account_chats
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_chats_delete AFTER DELETE ON account_chats
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_messages_insert AFTER INSERT ON account_messages
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_messages_update AFTER UPDATE ON account_messages
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_messages_delete AFTER DELETE ON account_messages
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_entity_tombstones_insert AFTER INSERT ON entity_tombstones
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_entity_tombstones_update AFTER UPDATE ON entity_tombstones
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_entity_tombstones_delete AFTER DELETE ON entity_tombstones
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_deletion_barriers_insert AFTER INSERT ON deletion_barriers
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_deletion_barriers_update AFTER UPDATE ON deletion_barriers
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_deletion_barriers_delete AFTER DELETE ON deletion_barriers
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_participant_deletion_chat_scopes_insert AFTER INSERT ON participant_deletion_chat_scopes
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_participant_deletion_chat_scopes_update AFTER UPDATE ON participant_deletion_chat_scopes
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_participant_deletion_chat_scopes_delete AFTER DELETE ON participant_deletion_chat_scopes
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;
