"""Receipt failures cannot change durable application transactions."""

import json
import os
import queue

import pytest

from app.core import lifecycle_receipts as receipts
from app.persistence.database import CanonicalSQLite

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.fixture
def sink(monkeypatch):
    reader, writer = os.pipe()
    observer = receipts.ReceiptSink(writer, "a" * 64)
    monkeypatch.setattr(receipts, "_sink", observer)
    yield observer, reader
    observer.finish(True)
    os.close(reader)


def read_all(reader):
    result = bytearray()
    while block := os.read(reader, 65536):
        result.extend(block)
    return [json.loads(line) for line in result.splitlines()]


def test_only_durable_commits_emit_and_identifiers_are_opaque(sink, tmp_path):
    observer, reader = sink
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    with database.transaction() as connection:
        connection.execute("CREATE TABLE events (value TEXT)")
    with pytest.raises(ValueError):
        with database.transaction() as connection:
            connection.execute("INSERT INTO events VALUES ('rolled back')")
            receipts.stage(connection, "canonical_commit", account_id="private-account",
                           canonical_revision=1)
            raise ValueError("rollback")
    with database.transaction() as connection:
        connection.execute("INSERT INTO events VALUES ('committed')")
        receipts.stage(connection, "canonical_commit", account_id="private-account",
                       canonical_revision=2)
        assert observer.sequence == 0
    with database.read() as connection:
        assert connection.execute("SELECT value FROM events").fetchall()[0][0] == "committed"
    observer.finish(True)
    records = read_all(reader)
    assert [record["event_type"] for record in records] == ["canonical_commit", "footer"]
    assert records[0]["canonical_revision"] == 2
    assert len(records[0]["account_ref"]) == 64
    assert "private-account" not in json.dumps(records)
    assert records[-1]["complete"] is True


def test_observation_error_preserves_commit_and_invalidates_footer(sink, tmp_path):
    observer, reader = sink
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    with database.transaction() as connection:
        connection.execute("CREATE TABLE events (value INTEGER)")
        receipts.begin_canonical(connection, "account", "authorized_agent", "event")
        connection.execute("INSERT INTO events VALUES (1)")
    with database.read() as connection:
        assert connection.execute("SELECT value FROM events").fetchone()[0] == 1
    observer.finish(True)
    assert read_all(reader)[-1]["complete"] is False


def test_queue_overflow_and_unjoined_workers_cannot_report_complete(sink, monkeypatch):
    observer, reader = sink
    original = observer.queue.put_nowait
    def full(record):
        raise queue.Full
    monkeypatch.setattr(observer.queue, "put_nowait", full)
    receipts.emit("canonical_commit", canonical_revision=1)
    monkeypatch.setattr(observer.queue, "put_nowait", original)
    observer.finish(False)
    records = read_all(reader)
    assert records[-1]["sequence"] == 2
    assert records[-1]["complete"] is False


def test_closed_pipe_does_not_raise_in_observed_code(monkeypatch):
    reader, writer = os.pipe()
    os.close(reader)
    observer = receipts.ReceiptSink(writer, "b" * 64)
    monkeypatch.setattr(receipts, "_sink", observer)
    receipts.emit("startup", clock_resolution_ns=1)
    observer.finish(True)
    assert observer.failed


def test_footer_terminates_writer_with_one_remaining_queue_slot(sink, monkeypatch):
    observer, reader = sink
    original = observer.queue.put_nowait
    remaining = 1
    def one_slot(record):
        nonlocal remaining
        if remaining == 0:
            raise queue.Full
        remaining -= 1
        original(record)
    monkeypatch.setattr(observer.queue, "put_nowait", one_slot)
    observer.finish(True)
    assert not observer.thread.is_alive()
    assert not observer.failed
    assert read_all(reader)[-1]["complete"] is True


def test_disabled_receipts_do_not_read_canonical_storage(monkeypatch):
    monkeypatch.setattr(receipts, "_sink", None)
    receipts.begin_canonical(object(), "account", "authorized_agent", "event")
    receipts.end_canonical(object(), 1)


def test_history_and_creator_deletion_emit_only_committed_changes(sink, tmp_path, monkeypatch):
    """Accepted history and creator deletion bind receipts to durable revisions."""
    from datetime import datetime, timezone

    from app.persistence.factory import create_canonical_repositories
    from app.persistence.retention import CreatorVaultRetention
    from tests.test_history_v2 import (
        ACCOUNT_ID, commit, commit_base_snapshot, delta, raw_message,
    )

    observer, reader = sink
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )
    database, history = repositories.database, repositories.history
    original_stage, original_committed = receipts.stage, receipts.committed
    staged_sequences, durable_states = [], []

    def stage(connection, event_type, *, account_id, **fields):
        assert connection.in_transaction
        sequence = observer.sequence
        original_stage(connection, event_type, account_id=account_id, **fields)
        assert observer.sequence == sequence
        staged_sequences.append(sequence)

    def committed(connection):
        pending = getattr(connection, "_lifecycle_receipts", ())
        if pending:
            assert not connection.in_transaction
            with database.read() as durable:
                revision = durable.execute(
                    "SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
                    (ACCOUNT_ID,),
                ).fetchone()[0]
                count = durable.execute(
                    "SELECT COUNT(*) FROM account_messages WHERE creator_account_id=?",
                    (ACCOUNT_ID,),
                ).fetchone()[0]
            assert pending[0][1]["canonical_revision"] == revision
            assert pending[0][1]["after_message_count"] == count
            durable_states.append((revision, count))
        original_committed(connection)

    monkeypatch.setattr(receipts, "stage", stage)
    monkeypatch.setattr(receipts, "committed", committed)
    sent_at = datetime.now(timezone.utc).isoformat()
    key, snapshot_id = commit_base_snapshot(
        history, messages=[raw_message("message-1", sent_at=sent_at)]
    )
    assert observer.sequence == 1
    assert history.commit_snapshot(key, commit(snapshot_id, 2)).status == "duplicate"
    assert observer.sequence == 1
    payload = delta(1, {
        "type": "message.upsert", "message": raw_message("message-2", sent_at=sent_at),
    })
    assert history.commit_delta(key, payload).status == "accepted"
    assert observer.sequence == 2
    assert history.commit_delta(key, payload).status == "duplicate"
    assert observer.sequence == 2
    CreatorVaultRetention(database).delete_message(ACCOUNT_ID, "message-2")
    assert observer.sequence == 3

    observer.finish(True)
    records = read_all(reader)
    assert staged_sequences == [0, 1, 2]
    assert durable_states == [(1, 1), (2, 2), (3, 1)]
    assert [record["event_type"] for record in records] == ["canonical_commit"] * 3 + ["footer"]
    assert [record["canonical_revision"] for record in records[:-1]] == [1, 2, 3]
    assert [record["origin"] for record in records[:-1]] == [
        "authorized_agent", "authorized_agent", "creator_vault",
    ]
    assert [(record["before_message_count"], record["after_message_count"])
            for record in records[:-1]] == [(0, 1), (1, 2), (2, 1)]
    assert all(record["admitted_events"] == 1 for record in records[:-1])
    assert len({record["source_event_ref"] for record in records[:-1]}) == 3
    assert records[-1]["complete"] is True
