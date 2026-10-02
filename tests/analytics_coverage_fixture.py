"""Construct declared acquisition facts without supplying message semantics."""


def seed_coverage(db, account, chats, at, *, generation="synthetic-coverage", state="complete"):
    instant = at.isoformat()
    db.execute("""INSERT INTO coverage_generations(
        creator_account_id,generation_id,authorization_revision,as_of,state,
        inventory_ended_at,closed_at) VALUES (?,?,'synthetic-consent',?,?,?,?)""",
        (account, generation, instant, state, instant, instant))
    db.executemany("""INSERT INTO coverage_members(
        creator_account_id,generation_id,conversation_id,history_started_at,head_reconciled_through)
        VALUES (?,?,?,?,?)""", [(account, generation, chat, instant, instant) for chat in chats])
    db.execute("""INSERT INTO account_coverage_heads(
        creator_account_id,active_generation_id,last_complete_generation_id,coverage_revision,updated_at)
        VALUES (?,?,?,1,?) ON CONFLICT(creator_account_id) DO UPDATE SET
        active_generation_id=excluded.active_generation_id,
        last_complete_generation_id=excluded.last_complete_generation_id,
        coverage_revision=coverage_revision+1,updated_at=excluded.updated_at""",
        (account, generation, generation if state == "complete" else None, instant))
