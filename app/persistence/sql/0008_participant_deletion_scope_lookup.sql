-- Optimize the ordinary-ingress deletion guard lookup used by chat, message,
-- snapshot, and raw-ingest retention fences. The participant-scope primary key
-- is ordered (creator_account_id, participant_scope_key, chat_id), which cannot
-- directly serve lookups constrained only by creator account and chat.
CREATE INDEX participant_deletion_chat_scopes_account_chat
    ON participant_deletion_chat_scopes (
        creator_account_id,
        chat_id
    );
