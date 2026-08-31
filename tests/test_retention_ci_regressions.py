from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention


ACCOUNT = "creator-account"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def database(path: Path) -> CanonicalSQLite:
    value = CanonicalSQLite(path)
    MigrationRunner(value).run()
    return value


def seed_raw_event(db: CanonicalSQLite, payload: dict[str, object]) -> str:
    installation_id = str(uuid4())
    stream_id = str(uuid4())
    event_id = str(uuid4())
    now = NOW.isoformat()
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO ingest_streams(
                   creator_account_id,agent_installation_id,agent_stream_id,created_at
               ) VALUES (?,?,?,?)""",
            (ACCOUNT, installation_id, stream_id, now),
        )
        connection.execute(
            """INSERT INTO raw_ingest_events(
                   creator_account_id,agent_installation_id,agent_stream_id,event_id,source_seq,
                   origin,observed_at,fingerprint,event_json,committed_at
               ) VALUES (?,?,?,?,1,'passive',?,'fingerprint',?,?)""",
            (ACCOUNT, installation_id, stream_id, event_id, now,
             json.dumps(payload, separators=(",", ":"), sort_keys=True), now),
        )
    return event_id


def raw_event_ids(db: CanonicalSQLite) -> set[str]:
    with db.read() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT event_id FROM raw_ingest_events WHERE creator_account_id=?",
                (ACCOUNT,),
            )
        }


@pytest.mark.parametrize(
    ("scope", "scope_key", "matching_payload"),
    [
        ("message", "message-1", {"envelope": {"message": {"message_id": "message-1"}}}),
        ("conversation", "chat-1", {"records": [{"chat_id": "chat-1"}]}),
        ("participant", "participant-1", {"message": {"sender_platform_user_id": "participant-1"}}),
    ],
)
def test_selective_deletion_barrier_scrubs_matching_raw_ingest_only(
    tmp_path: Path,
    scope: str,
    scope_key: str,
    matching_payload: dict[str, object],
) -> None:
    db = database(tmp_path / f"{scope}.sqlite3")
    matching = seed_raw_event(db, matching_payload)
    unrelated = seed_raw_event(db, {"message": {"message_id": "keep-me"}})
    retention = CreatorVaultRetention(db, clock=lambda: NOW)

    if scope == "message":
        retention.delete_message(ACCOUNT, scope_key)
    elif scope == "conversation":
        retention.delete_conversation(ACCOUNT, scope_key)
    else:
        retention.delete_participant(ACCOUNT, scope_key)

    assert raw_event_ids(db) == {unrelated}
    assert matching not in raw_event_ids(db)


def test_account_deletion_barrier_scrubs_all_raw_ingest(tmp_path: Path) -> None:
    db = database(tmp_path / "account.sqlite3")
    seed_raw_event(db, {"message": {"message_id": "message-1"}})
    seed_raw_event(db, {"records": [{"chat_id": "chat-1"}]})

    CreatorVaultRetention(db, clock=lambda: NOW).delete_all(ACCOUNT)

    assert raw_event_ids(db) == set()
