"""Resolve synthetic source records through the encrypted canonical gateway."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.evidence import EvidenceResolver, EvidenceUnavailable
from app.analytics.evidence_contracts import EvidenceLocation, EvidenceMessage
from app.analytics.query_contracts import SourceSpan
from app.analytics.query_execution import QuestionBudget, QuestionCancelled, QuestionLimits
from app.persistence.factory import create_canonical_repositories
from app.security.runtime_policy import (
    AuthContext, AuthorizationEpoch, RuntimeAuthorizationDenied, RuntimePolicy,
)

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
ACCOUNT = "synthetic-evidence-owner"
OTHER = "synthetic-evidence-other"
TEXT = "A\U0001f44b price \u20ac20"
LOCATION = EvidenceLocation(conversation_id="synthetic-chat-a", message_id="synthetic-m1")


def policy(account=ACCOUNT):
    return RuntimePolicy(AuthContext("synthetic-principal", account, "creator"),
                         AuthorizationEpoch(1))


@pytest.fixture(params=["memory", "sqlite"])
def stored(request, tmp_path):
    kwargs = {"canonical_path": tmp_path / "canonical.sqlite3"} if request.param == "sqlite" else {}
    repositories = create_canonical_repositories(request.param, **kwargs)
    with repositories.database.transaction() as db:
        for account in (ACCOUNT, OTHER):
            db.execute("INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)",
                       (account, 7, NOW.isoformat()))
            for index in (1, 2):
                chat = "synthetic-chat-" + ("a" if index == 1 else "b")
                db.execute("""INSERT INTO account_chats(
                    creator_account_id,chat_id,record_kind,platform_user_id,content_hash,
                    winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
                    VALUES (?,?,'full','synthetic-participant','synthetic-chat-hash',1,1,0,?)""",
                           (account, chat, NOW.isoformat()))
                db.execute("""INSERT INTO account_messages(
                    creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                    direction,content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
                    VALUES (?,?,?,'synthetic-participant',?,?,'inbound','synthetic-hash',1,1,0,?)""",
                           (account, f"synthetic-m{index}", chat, TEXT,
                            (NOW - timedelta(hours=1)).isoformat(), NOW.isoformat()))
    return repositories


def budget():
    return QuestionBudget(QuestionLimits(wall_clock_ms=30_000))


def prepared(stored, *, location=LOCATION, span=None, **kwargs):
    source = HistoryAnalyticsSource(stored.history)
    record = source.read_evidence_message(ACCOUNT, location, budget())
    assert record is not None
    ref = record.reference(span)
    resolver = EvidenceResolver(source, clock=kwargs.pop("clock", lambda: NOW), **kwargs)
    resolver.bind(policy(), ref, location, valid_until=NOW + timedelta(days=1))
    return resolver, ref, source


def test_source_resolution_returns_exact_message_and_browser_span(stored):
    resolver, ref, _ = prepared(stored, span=SourceSpan(start=1, end=2))
    result = resolver.resolve(policy(), ref)
    assert result.text == TEXT
    assert result.location == LOCATION
    assert result.reference == ref and result.checked_at == NOW
    assert result.browser_span == SourceSpan(start=1, end=3)
    assert TEXT not in repr(result)
    assert TEXT not in repr(resolver._entries)
    assert "synthetic-m1" not in repr(result)


def test_text_and_metadata_affect_version_but_account_revision_does_not(stored):
    _, ref, source = prepared(stored)
    record = source.read_evidence_message(ACCOUNT, LOCATION, budget())
    changed = record.model_copy(update={"source_revision": 8}).reference()
    assert changed.source_version_digest == ref.source_version_digest


@pytest.mark.parametrize("column,value", [
    ("text", "synthetic replacement"), ("direction", "outbound"),
    ("sender_platform_user_id", "synthetic-different-sender"),
    ("sent_at", "2026-09-18T10:00:00Z"),
    ("upstream_updated_at", "2026-09-18T11:30:00Z"),
    ("content_hash", "synthetic-changed-hash"),
    ("winning_source_seq", 2), ("winning_stream_epoch", 2),
])
def test_changed_source_never_displays_replacement_text(stored, column, value):
    refreshes = []
    resolver, ref, _ = prepared(stored, request_refresh=refreshes.append)
    with stored.database.transaction() as db:
        db.execute(f"UPDATE account_messages SET {column}=? WHERE creator_account_id=? AND message_id=?",
                   (value, ACCOUNT, LOCATION.message_id))
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    assert refreshes == [ACCOUNT]
    assert not resolver._entries


@pytest.mark.parametrize("scope,key", [
    ("account", "*"), ("conversation", "synthetic-chat-a"),
    ("message", "synthetic-m1"), ("participant", "synthetic-participant"),
])
def test_deletion_barrier_refuses_a_source_even_when_rows_remain(stored, scope, key):
    resolver, ref, _ = prepared(stored)
    with stored.database.transaction() as db:
        db.execute("INSERT INTO deletion_barriers VALUES (?,?,?,?,?,?)",
                   (ACCOUNT, scope, key, 1, NOW.isoformat(), "synthetic-test"))
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    assert not resolver._entries


@pytest.mark.parametrize("kind", ["message", "chat", "hard_message", "hard_account", "revision", "participant_scope"])
def test_deleted_or_revised_canonical_state_invalidates_evidence(stored, kind):
    resolver, ref, _ = prepared(stored)
    with stored.database.transaction() as db:
        if kind == "revision":
            db.execute("UPDATE account_heads SET canonical_revision=8 WHERE creator_account_id=?", (ACCOUNT,))
        elif kind == "hard_message":
            db.execute("DELETE FROM account_messages WHERE creator_account_id=?", (ACCOUNT,))
        elif kind == "hard_account":
            db.execute("DELETE FROM account_heads WHERE creator_account_id=?", (ACCOUNT,))
        elif kind == "participant_scope":
            db.execute("INSERT INTO participant_deletion_chat_scopes VALUES (?,?,?,?,?)",
                       (ACCOUNT, "synthetic-deleted-participant", LOCATION.conversation_id, 1, NOW.isoformat()))
        else:
            db.execute("INSERT INTO entity_tombstones VALUES (?,?,?,?,?,?,?,?)",
                       (ACCOUNT, kind, LOCATION.message_id if kind == "message" else LOCATION.conversation_id,
                        LOCATION.conversation_id, 1, 2, "synthetic-delete", NOW.isoformat()))
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)


@pytest.mark.parametrize("fault", ["account", "conversation", "message", "digest", "revision", "time", "span", "raw_text"])
def test_forged_reference_does_not_reach_storage(stored, fault, monkeypatch):
    resolver, ref, source = prepared(stored)
    changes = {
        "account": {"account_ref": ref.account_ref.replace("a1:", "a1:0", 1)},
        "conversation": {"conversation_ref": "c1:" + "0" * 64},
        "message": {"message_ref": "m1:" + "0" * 64},
        "digest": {"source_version_digest": "sha256:" + "0" * 64},
        "revision": {"source_revision": 8},
        "time": {"sent_at": NOW.isoformat()},
        "span": {"span": {"start": 0, "end": 1}},
        "raw_text": {"text": "synthetic injected text"},
    }
    def forbidden(*args):
        pytest.fail("forged reference reached canonical storage")
    monkeypatch.setattr(source, "read_evidence_message", forbidden)
    altered = ref.model_dump(mode="json") | changes[fault]
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), altered)


def test_account_switch_and_unauthenticated_request_return_no_text(stored, monkeypatch):
    resolver, ref, source = prepared(stored)
    def forbidden(*args):
        pytest.fail("unauthorized reference reached storage")
    monkeypatch.setattr(source, "read_evidence_message", forbidden)
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(OTHER), ref)
    with pytest.raises(RuntimeAuthorizationDenied):
        resolver.resolve(RuntimePolicy(None, AuthorizationEpoch(1)), ref)


def test_same_native_ids_are_isolated_by_account(stored):
    resolver, ref, source = prepared(stored)
    other = source.read_evidence_message(OTHER, LOCATION, budget()).reference()
    assert other.message_ref != ref.message_ref
    assert other.source_version_digest != ref.source_version_digest
    resolver.bind(policy(OTHER), other, LOCATION, valid_until=NOW + timedelta(days=1))
    resolver.clear_account(policy())
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    assert resolver.resolve(policy(OTHER), other).reference == other


@pytest.mark.parametrize("days", [90, 91])
def test_expired_source_cannot_be_bound_even_when_canonical_data_remains(stored, days):
    source = HistoryAnalyticsSource(stored.history)
    with stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET sent_at=? WHERE creator_account_id=?",
                   ((NOW - timedelta(days=days)).isoformat(), ACCOUNT))
    message = source.read_evidence_message(ACCOUNT, LOCATION, budget())
    resolver = EvidenceResolver(source, clock=lambda: NOW)
    with pytest.raises(EvidenceUnavailable):
        resolver.bind(policy(), message.reference(), LOCATION, valid_until=NOW + timedelta(days=1))
    assert not resolver._entries


def test_input_expiry_and_locator_lifetime_are_not_renewed_by_reads(stored):
    clock = [NOW]
    resolver, ref, _ = prepared(stored, clock=lambda: clock[0])
    clock[0] += timedelta(minutes=14)
    assert resolver.resolve(policy(), ref).text == TEXT
    clock[0] += timedelta(minutes=1)
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    assert not resolver._entries
    resolver.bind(policy(), ref, LOCATION, valid_until=clock[0] + timedelta(seconds=1))
    clock[0] += timedelta(seconds=1)
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)


def test_expiry_without_reads_has_an_explicit_maintenance_hook(stored):
    clock = [NOW]
    resolver, _, _ = prepared(stored, clock=lambda: clock[0])
    clock[0] += timedelta(minutes=15)
    assert resolver.expire() == 1
    assert resolver.expire() == 0


def test_monotonic_deadline_and_clock_rollback_fail_closed(stored):
    clock, tick = [NOW], [100.0]
    resolver, ref, _ = prepared(stored, clock=lambda: clock[0], monotonic=lambda: tick[0])
    tick[0] += 900
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    resolver.bind(policy(), ref, LOCATION, valid_until=NOW + timedelta(days=1))
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)


def test_eviction_and_restart_require_reissuing_query_references(stored):
    resolver, first, source = prepared(stored, max_entries=1)
    second_location = EvidenceLocation(conversation_id="synthetic-chat-b", message_id="synthetic-m2")
    second = source.read_evidence_message(ACCOUNT, second_location, budget()).reference()
    resolver.bind(policy(), second, second_location, valid_until=NOW + timedelta(days=1))
    assert len(resolver._entries) == 1
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), first)
    assert resolver.resolve(policy(), second).location == second_location
    with pytest.raises(EvidenceUnavailable):
        EvidenceResolver(source, clock=lambda: NOW).resolve(policy(), second)


@pytest.mark.parametrize("fault", ["edit", "delete", "revision", "clear", "expiry"])
def test_changes_during_resolution_suppress_the_response(stored, fault, monkeypatch):
    clock = [NOW]
    resolver, ref, source = prepared(stored, clock=lambda: clock[0])
    read = source.read_evidence_message
    calls = []
    def changes(account, location, limit):
        calls.append(1)
        result = read(account, location, limit)
        if len(calls) == 1:
            if fault == "clear":
                resolver.clear_account(policy())
            elif fault == "expiry":
                clock[0] += timedelta(days=90)
            else:
                with stored.database.transaction() as db:
                    if fault == "edit":
                        db.execute("UPDATE account_messages SET text='synthetic changed' WHERE creator_account_id=?", (ACCOUNT,))
                    elif fault == "delete":
                        db.execute("DELETE FROM account_messages WHERE creator_account_id=?", (ACCOUNT,))
                    else:
                        db.execute("UPDATE account_heads SET canonical_revision=8 WHERE creator_account_id=?", (ACCOUNT,))
        return result
    monkeypatch.setattr(source, "read_evidence_message", changes)
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)


def test_cancelled_resolution_does_not_read_storage(stored, monkeypatch):
    resolver, ref, source = prepared(stored)
    def forbidden(*args):
        pytest.fail("cancelled resolution reached storage")
    monkeypatch.setattr(source, "read_evidence_message", forbidden)
    with pytest.raises(QuestionCancelled):
        resolver.resolve(policy(), ref, cancellation_check=lambda: True)


def test_adapter_exception_and_refresh_failure_do_not_disclose_content(stored, monkeypatch):
    def fails(*args):
        raise RuntimeError("synthetic-secret-error-payload")
    resolver, ref, source = prepared(stored, request_refresh=fails)
    monkeypatch.setattr(source, "read_evidence_message", fails)
    with pytest.raises(EvidenceUnavailable) as error:
        resolver.resolve(policy(), ref)
    assert "synthetic-secret-error-payload" not in str(error.value)
    assert error.value.__suppress_context__
    assert not resolver._entries


@pytest.mark.parametrize("fault", ["location", "old_version", "span"])
def test_invalid_binding_cannot_replace_a_source_version(stored, fault):
    resolver, ref, _ = prepared(stored)
    location = LOCATION
    if fault == "location":
        location = location.model_copy(update={"conversation_id": "synthetic-other-chat"})
    elif fault == "old_version":
        ref = ref.model_copy(update={"source_version_digest": "sha256:" + "0" * 64})
    else:
        ref = ref.model_copy(update={"span": SourceSpan(start=0, end=1000)})
    with pytest.raises(EvidenceUnavailable):
        resolver.bind(policy(), ref, location, valid_until=NOW + timedelta(days=1))


def test_snapshot_bound_gateway_is_not_accepted_as_live_evidence(stored):
    with stored.database.read() as connection:
        source = HistoryAnalyticsSource(stored.history, connection=connection)
        with pytest.raises(ValueError, match="evidence_live_read_required"):
            source.read_evidence_message(ACCOUNT, LOCATION, budget())


def test_large_message_returns_unavailable_without_changing_canonical_text(stored):
    resolver, ref, _ = prepared(stored)
    with stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET text=? WHERE creator_account_id=?", ("x" * 65_537, ACCOUNT))
    with pytest.raises(EvidenceUnavailable):
        resolver.resolve(policy(), ref)
    with stored.database.read() as db:
        assert db.execute("SELECT length(text) FROM account_messages WHERE creator_account_id=? LIMIT 1", (ACCOUNT,)).fetchone()[0] == 65_537


@pytest.mark.parametrize("capacity", [0, -1, True, 32769])
def test_cache_capacity_is_bounded(capacity):
    with pytest.raises(ValueError, match="evidence_capacity_invalid"):
        EvidenceResolver(None, max_entries=capacity)


def test_resolver_uses_two_indexed_live_reads_not_account_materialization(stored, monkeypatch):
    resolver, ref, source = prepared(stored)
    original_read = stored.database.read
    selects, plans, progress_handlers = [], [], []

    class CheckedConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, parameters=()):
            if sql.lstrip().startswith("SELECT h.canonical_revision"):
                selects.append(sql)
                plans.extend(str(row[3]) for row in self.connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))
            return self.connection.execute(sql, parameters)
        def set_progress_handler(self, callback, frequency):
            progress_handlers.append((callback is not None, frequency))
            return self.connection.set_progress_handler(callback, frequency)

    @contextmanager
    def checked_read():
        with original_read() as connection:
            yield CheckedConnection(connection)

    def forbidden(*args):
        pytest.fail("evidence lookup materialized canonical account")
    monkeypatch.setattr(stored.database, "read", checked_read)
    monkeypatch.setattr(source, "account_read_model", forbidden)
    assert resolver.resolve(policy(), ref).text == TEXT
    assert len(selects) == 2
    assert all("LIMIT 1" in sql for sql in selects)
    assert any("SEARCH m USING INDEX" in plan for plan in plans)
    assert not any("SCAN m" in plan for plan in plans)
    assert progress_handlers == [(True, 100), (False, 0), (True, 100), (False, 0)]


def test_gateway_cancellation_restores_the_progress_handler(stored, monkeypatch):
    source = HistoryAnalyticsSource(stored.history)
    original_read = stored.database.read
    callbacks = []
    class CancelledConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, parameters=()):
            return self.connection.execute(sql, parameters)
        def set_progress_handler(self, callback, frequency):
            callbacks.append(callback is not None)
            if callback is not None:
                def cancelled():
                    cancelled_flag[0] = True
                    return callback()
                self.connection.set_progress_handler(cancelled, frequency)
            else:
                self.connection.set_progress_handler(None, 0)
    @contextmanager
    def cancelled_read():
        with original_read() as connection:
            yield CancelledConnection(connection)
    cancelled_flag = [False]
    monkeypatch.setattr(stored.database, "read", cancelled_read)
    limit = QuestionBudget(QuestionLimits(), cancellation_check=lambda: cancelled_flag[0])
    with pytest.raises(QuestionCancelled):
        source.read_evidence_message(ACCOUNT, LOCATION, limit)
    assert callbacks == [True, False]


def test_sparse_attachment_text_can_be_resolved_without_a_model(stored):
    with stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='' WHERE creator_account_id=?", (ACCOUNT,))
    resolver, ref, _ = prepared(stored)
    result = resolver.resolve(policy(), ref)
    assert result.text == "" and result.browser_span is None



def test_clearing_an_account_fences_inflight_binding(stored, monkeypatch):
    resolver, ref, source = prepared(stored)
    original = source.read_evidence_message
    def cleared(account, location, limit):
        record = original(account, location, limit)
        resolver.clear_account(policy())
        return record
    monkeypatch.setattr(source, "read_evidence_message", cleared)
    with pytest.raises(EvidenceUnavailable):
        resolver.bind(policy(), ref, LOCATION, valid_until=NOW + timedelta(days=1))
    assert not resolver._entries


def test_embedded_null_does_not_bypass_source_byte_limit(stored, monkeypatch):
    with stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET text=? WHERE creator_account_id=?", ("prefix\0" + "x" * 300_000, ACCOUNT))
    def forbidden(*args, **kwargs):
        pytest.fail("oversized text reached source model construction")
    monkeypatch.setattr("app.analytics.canonical_source.EvidenceMessage", forbidden)
    assert HistoryAnalyticsSource(stored.history).read_evidence_message(ACCOUNT, LOCATION, budget()) is None
