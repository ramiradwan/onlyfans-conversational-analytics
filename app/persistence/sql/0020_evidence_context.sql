CREATE INDEX account_messages_context
    ON account_messages (
        creator_account_id, chat_id, julianday(sent_at),
        winning_stream_epoch, winning_source_seq, message_id
    )
    WHERE is_deleted = 0;
