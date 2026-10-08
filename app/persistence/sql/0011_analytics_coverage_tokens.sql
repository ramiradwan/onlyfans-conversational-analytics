-- Bind acquisition evidence changes to cached source verification.

CREATE TRIGGER analytics_source_token_account_coverage_heads_insert AFTER INSERT ON account_coverage_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_coverage_heads_update AFTER UPDATE ON account_coverage_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_account_coverage_heads_delete AFTER DELETE ON account_coverage_heads
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_generations_insert AFTER INSERT ON coverage_generations
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_generations_update AFTER UPDATE ON coverage_generations
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_generations_delete AFTER DELETE ON coverage_generations
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_members_insert AFTER INSERT ON coverage_members
BEGIN
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_members_update AFTER UPDATE ON coverage_members
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
    INSERT INTO analytics_source_tokens VALUES (NEW.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;

CREATE TRIGGER analytics_source_token_coverage_members_delete AFTER DELETE ON coverage_members
BEGIN
    INSERT INTO analytics_source_tokens VALUES (OLD.creator_account_id,lower(hex(randomblob(16)))) ON CONFLICT(creator_account_id) DO UPDATE SET token=excluded.token;
END;
