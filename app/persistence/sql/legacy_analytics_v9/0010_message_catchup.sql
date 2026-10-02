CREATE TABLE message_catchup (
    creator_account_id TEXT PRIMARY KEY,
    gap_open INTEGER NOT NULL DEFAULT 1 CHECK (gap_open IN (0,1)),
    gap_epoch INTEGER NOT NULL DEFAULT 1 CHECK (gap_epoch >= 0),
    uncertain_since TEXT,
    pending_uncertain_since TEXT,
    last_closed_at TEXT,
    observing_since TEXT,
    last_observing_report_at TEXT,
    last_report_at TEXT,
    last_worker_instance_id TEXT,
    blind_catchup_done INTEGER NOT NULL DEFAULT 0,
    active_check_id TEXT,
    last_grant_at TEXT,
    next_canary_at TEXT,
    observing INTEGER NOT NULL DEFAULT 0,
    reported_observing INTEGER NOT NULL DEFAULT 0,
    report_reason TEXT NOT NULL DEFAULT 'hook_not_armed',
    supported INTEGER NOT NULL DEFAULT 0,
    session_present INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    disconnected_at TEXT,
    loss_recorded INTEGER NOT NULL DEFAULT 0,
    agent_installation_id TEXT,
    agent_stream_id TEXT,
    incomplete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE message_checks (
    check_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('catch_up','canary')),
    epoch INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    uncertain_since TEXT,
    blind INTEGER NOT NULL CHECK (blind IN (0,1)),
    lease_expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','suspended','completed','abandoned')),
    suspended_day TEXT,
    completed_at TEXT,
    reason TEXT,
    request_id TEXT,
    agent_installation_id TEXT,
    agent_stream_id TEXT,
    inventory_json TEXT,
    inserted_old INTEGER NOT NULL DEFAULT 0,
    final_source_seq INTEGER,
    completion_json TEXT
);
CREATE UNIQUE INDEX one_active_message_check ON message_checks(creator_account_id)
    WHERE state IN ('active','suspended');

CREATE TABLE message_check_chats (
    check_id TEXT NOT NULL REFERENCES message_checks(check_id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (check_id,chat_id)
);

CREATE TABLE capture_report_receipts (
    creator_account_id TEXT NOT NULL,
    worker_instance_id TEXT NOT NULL,
    report_seq INTEGER NOT NULL CHECK (report_seq > 0),
    PRIMARY KEY (creator_account_id,worker_instance_id)
);

CREATE TABLE capture_daily_counters (
    creator_account_id TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    automatic_pages INTEGER NOT NULL DEFAULT 0,
    canary_list INTEGER NOT NULL DEFAULT 0,
    catchup_list INTEGER NOT NULL DEFAULT 0,
    catchup_messages INTEGER NOT NULL DEFAULT 0,
    history_list INTEGER NOT NULL DEFAULT 0,
    history_messages INTEGER NOT NULL DEFAULT 0,
    identity INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    expired INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (creator_account_id,utc_day)
);

INSERT INTO message_catchup(creator_account_id,uncertain_since,last_closed_at)
SELECT h.creator_account_id,
       (SELECT MAX(g.closed_at) FROM coverage_generations g
        WHERE g.creator_account_id=h.creator_account_id AND
          (g.state='complete' OR g.generation_id=c.last_complete_generation_id)),
       (SELECT MAX(g.closed_at) FROM coverage_generations g
        WHERE g.creator_account_id=h.creator_account_id AND
          (g.state='complete' OR g.generation_id=c.last_complete_generation_id))
FROM account_heads h LEFT JOIN account_coverage_heads c USING(creator_account_id);
