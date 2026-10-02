"""Gap proofs, source fences and channel continuity use a simulated clock."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]

from app.persistence.factory import create_canonical_repositories
from app.persistence.history import InvariantViolation
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from test_history_v2 import (
    ACCOUNT_ID, INSTALLATION_ID, STREAM_ID, authorize_history, begin, chunk,
    commit, commit_base_snapshot, delta, identity, message, raw_message,
)


START = datetime(2026, 9, 29, tzinfo=timezone.utc)
WORKER = str(uuid4())


def report(at, seq=1, **changes):
    value = dict(
        worker_instance_id=WORKER, report_seq=seq, observing=True, reason="ok",
        tabs=dict(armed=1, frozen=0, discarded=0), page_socket_open=True,
        drops_since_last=dict(expired=0, rejected=0),
        requests_since_last=dict(canary_list=0, catchup_list=0, catchup_messages=0,
                                 history_list=0, history_messages=0, identity=0, retries=0),
        utc_day=at.date().isoformat(), automatic_pages_today=0,
    )
    value.update(changes)
    return value


def request(**changes):
    value = dict(request_id=str(uuid4()), worker_instance_id=WORKER, trigger="alarm",
                 config_revision="config-history-1", head_evidence="full", active_check_id=None)
    value.update(changes)
    return value


@pytest.fixture
def rig(tmp_path, monkeypatch):
    repos = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.db", projection_path=tmp_path / "projection.db",
    )
    h = repos.history
    clock = SimpleNamespace(now=START)
    monkeypatch.setattr("app.persistence.history.utc_now", lambda: clock.now)
    monkeypatch.setattr("app.transport.manager.utc_now", lambda: clock.now)
    key, _ = commit_base_snapshot(h)
    authorize_history(h)
    generation = str(uuid4())
    with h.database.transaction() as c:
        c.execute("INSERT INTO coverage_generations VALUES (?,?,?,?,'complete',?,?,NULL)",
                  (ACCOUNT_ID, generation, "consent-1", START.isoformat(), START.isoformat(), START.isoformat()))
        c.execute("UPDATE account_coverage_heads SET active_generation_id=?,last_complete_generation_id=? WHERE creator_account_id=?",
                  (generation, generation, ACCOUNT_ID))
    ledger = h.catchup
    ledger.admit(ACCOUNT_ID, installation=str(INSTALLATION_ID), stream=str(STREAM_ID),
                 supported=True, now=clock.now)
    ledger.report(ACCOUNT_ID, report(clock.now), now=clock.now)
    return SimpleNamespace(h=h, l=ledger, key=key, clock=clock, repos=repos)


def state(r):
    return r.l.state(ACCOUNT_ID, now=r.clock.now)


def row(r):
    with r.h.database.read() as c:
        return dict(c.execute("SELECT * FROM message_catchup WHERE creator_account_id=?", (ACCOUNT_ID,)).fetchone())


def grant(r, **changes):
    result = r.l.begin(ACCOUNT_ID, request(**changes), now=r.clock.now)
    assert result["result"] == "granted"
    return result


def evidence(r, check, evidence_type, **fields):
    seq = r.h.checkpoint(r.key) + 1
    payload = delta(seq, {"type": "coverage.observed", "evidence": {
        "type": evidence_type, "generation_id": check["check_id"], **fields,
    }})
    return r.h.commit_delta(r.key, payload)


def finish(r, check, *, heads=None, final=None):
    if check["kind"] == "catch_up":
        evidence(r, check, "check.inventory_closed", strategy="timestamp", scanned=0, changed=0, movers=0)
    fields = dict(kind=check["kind"], final_source_seq=r.h.checkpoint(r.key) if final is None else final,
                  pages_read=1, counts=dict(list=1, messages=0, probes=0))
    if heads is not None:
        fields["heads"] = heads
    return evidence(r, check, "check.completed", **fields)


def current(r):
    check = grant(r)
    finish(r, check)
    assert state(r)["status"] == "current"
    return check


def advance(r, seconds, seq=2):
    r.clock.now += timedelta(seconds=seconds)
    r.l.heartbeat(ACCOUNT_ID, now=r.clock.now)
    r.l.report(ACCOUNT_ID, report(r.clock.now, seq), now=r.clock.now)


def canary(r):
    current(r)
    with r.h.database.transaction() as c:
        c.execute("UPDATE message_catchup SET next_canary_at=?", (r.clock.now.isoformat(),))
    check = grant(r)
    assert check["kind"] == "canary"
    return check


def test_admission_never_closes_seeded_gap(rig):
    r = rig
    assert row(r)["uncertain_since"] == START.isoformat()
    assert state(r)["status"] == "behind"
    r.l.admit(ACCOUNT_ID, installation=str(INSTALLATION_ID), stream=str(STREAM_ID), supported=True, now=START)
    assert state(r)["status"] == "behind"
    current(r)


@pytest.mark.parametrize("trigger", ["worker", "not_observing", "drops", "restart", "account", "authorization", "consent", "stale", "lost"])
def test_every_trigger_preserves_boundary(rig, trigger):
    r = rig
    current(r)
    r.clock.now += timedelta(seconds=20)
    expected = START
    if trigger == "worker":
        r.l.report(ACCOUNT_ID, report(r.clock.now, worker_instance_id=str(uuid4())), now=r.clock.now)
    elif trigger == "not_observing":
        r.l.report(ACCOUNT_ID, report(r.clock.now, 2, observing=False, reason="hook_not_armed"), now=r.clock.now)
    elif trigger == "drops":
        r.l.report(ACCOUNT_ID, report(r.clock.now, 2, drops_since_last=dict(expired=1, rejected=0)), now=r.clock.now)
    elif trigger == "restart":
        r.l.restart(now=r.clock.now)
    elif trigger in {"account", "authorization", "consent"}:
        expected = r.clock.now
        r.l.epoch_changed(ACCOUNT_ID, now=r.clock.now)
    else:
        if trigger == "lost":
            r.l.disconnect(ACCOUNT_ID, now=START)
        r.clock.now = START + timedelta(seconds=61)
        r.l.expire(now=r.clock.now)
    assert row(r)["gap_open"] == 1
    assert row(r)["uncertain_since"] == expected.isoformat()


def test_epoch_during_check_stays_open_at_pending_boundary(rig):
    r = rig
    check = grant(r)
    r.clock.now += timedelta(seconds=10)
    r.l.epoch_changed(ACCOUNT_ID, now=r.clock.now)
    boundary = r.clock.now.isoformat()
    assert row(r)["gap_epoch"] == check["gap_epoch"] + 1
    finish(r, check)
    assert state(r)["status"] == "behind"
    assert row(r)["uncertain_since"] == boundary
    assert row(r)["last_closed_at"] == START.isoformat()


def test_blind_completion_never_closes_or_moves_boundary(rig):
    r = rig
    r.l.report(ACCOUNT_ID, report(START, 2, observing=False, reason="hook_not_armed"), now=START)
    before = row(r)["uncertain_since"]
    check = grant(r)
    assert check["blind"]
    finish(r, check)
    assert row(r)["gap_open"] == 1
    assert row(r)["uncertain_since"] == before
    assert row(r)["blind_catchup_done"] == 1
    assert r.l.begin(ACCOUNT_ID, request(), now=START)["result"] != "granted"
    r.l.report(ACCOUNT_ID, report(START, 3), now=START)
    assert row(r)["blind_catchup_done"] == 0


@pytest.mark.parametrize("reason", ["capture_off", "consent_needed", "paused", "account_mismatch", "storage_locked", "no_onlyfans_tab", "tab_frozen", "tab_discarded"])
def test_grant_requires_runnable_chain(rig, reason):
    rig.l.report(ACCOUNT_ID, report(START, 2, observing=False, reason=reason), now=START)
    result = rig.l.begin(ACCOUNT_ID, request(), now=START)
    assert result["result"] == "deferred" and result["reason"] == "not_runnable"
    assert state(rig)["status"] == "paused"


@pytest.mark.parametrize("condition,reason", [("offline", "not_runnable"), ("history", "history_incomplete"), ("config", "not_runnable"), ("active", "check_active"), ("cap", "daily_cap")])
def test_grant_preconditions(rig, condition, reason):
    r = rig
    req = request()
    if condition == "offline":
        r.clock.now += timedelta(seconds=61)
    elif condition == "history":
        with r.h.database.transaction() as c:
            c.execute("UPDATE account_coverage_heads SET last_complete_generation_id=NULL")
            c.execute("UPDATE coverage_generations SET state='partial'")
    elif condition == "config":
        req["config_revision"] = "not-applied"
    elif condition == "active":
        grant(r)
    else:
        r.l.report(ACCOUNT_ID, report(START, 2, automatic_pages_today=1000), now=START)
    result = r.l.begin(ACCOUNT_ID, req, now=r.clock.now)
    assert result["result"] == "deferred" and result["reason"] == reason


def test_ten_minute_rule_applies_only_to_new_grants(rig):
    r = rig
    check = grant(r)
    r.clock.now += timedelta(seconds=1)
    renewed = grant(r, trigger="renew", active_check_id=check["check_id"])
    assert renewed["check_id"] == check["check_id"] and renewed["resume"]
    assert renewed["lease_expires_at"] > check["lease_expires_at"]
    finish(r, check)
    r.l.epoch_changed(ACCOUNT_ID, now=r.clock.now)
    result = r.l.begin(ACCOUNT_ID, request(), now=r.clock.now)
    assert result["reason"] == "grant_interval"
    for seq in range(2, 12):
        advance(r, 60, seq)
    assert grant(r)["check_id"] != check["check_id"]


def test_cap_suspends_lease_and_resumes_next_day(rig):
    r = rig
    check = grant(r)
    r.l.report(ACCOUNT_ID, report(START, 2, automatic_pages_today=1000), now=START)
    result = r.l.begin(ACCOUNT_ID, request(trigger="renew", active_check_id=check["check_id"]), now=START)
    assert result["reason"] == "daily_cap"
    r.clock.now += timedelta(seconds=301)
    r.l.expire(now=r.clock.now)
    assert row(r)["active_check_id"] == check["check_id"]
    r.clock.now = START + timedelta(days=1)
    r.l.heartbeat(ACCOUNT_ID, now=r.clock.now)
    r.l.report(ACCOUNT_ID, report(r.clock.now, 3), now=r.clock.now)
    renewed = grant(r, trigger="renew", active_check_id=check["check_id"])
    assert renewed["check_id"] == check["check_id"]
    assert renewed["page_budget"] == 200


def test_lease_expiry_abandons_without_closing(rig):
    r = rig
    check = grant(r)
    r.clock.now += timedelta(seconds=301)
    r.l.expire(now=r.clock.now)
    assert row(r)["active_check_id"] is None and row(r)["gap_open"]
    with r.h.database.read() as c:
        saved = c.execute("SELECT state,reason FROM message_checks WHERE check_id=?", (check["check_id"],)).fetchone()
    assert tuple(saved) == ("abandoned", "lease_expired")


def test_report_retries_do_not_double_count_or_refresh_observation(rig):
    r = rig
    value = report(START, 2, automatic_pages_today=3)
    value["requests_since_last"]["catchup_list"] = 3
    assert r.l.report(ACCOUNT_ID, value, now=START) == {"acknowledged_seq": 2}
    r.clock.now += timedelta(seconds=20)
    r.l.report(ACCOUNT_ID, value, now=r.clock.now)
    assert row(r)["last_report_at"] == START.isoformat()
    counters = r.l.counters(now=START)
    assert counters["catchup_list"] == 3 and counters["automatic_pages"] == 3
    assert ACCOUNT_ID not in json.dumps(counters)


def test_stale_report_opens_gap_at_last_observing_report(rig):
    r = rig
    current(r)
    r.clock.now += timedelta(seconds=91)
    r.l.heartbeat(ACCOUNT_ID, now=START + timedelta(seconds=50))
    r.l.heartbeat(ACCOUNT_ID, now=r.clock.now)
    assert row(r)["gap_open"]
    assert row(r)["uncertain_since"] == START.isoformat()


@pytest.mark.parametrize("head, mismatch", [
    ({"chat_id": "chat-1", "head_message_id": "message-1"}, False),
    ({"chat_id": "chat-1", "head_message_id": "missing"}, True),
    ({"chat_id": "chat-1", "head_sent_at": "2026-09-28T12:00:00Z"}, True),
    ({"chat_id": "chat-1", "head_message_id": "missing", "head_sent_at": "2026-09-29T00:00:00Z"}, False),
])
def test_canary_head_evaluation(rig, head, mismatch):
    check = canary(rig)
    finish(rig, check, heads=[head])
    assert bool(row(rig)["gap_open"]) is mismatch
    assert row(rig)["next_canary_at"] == (START + timedelta(hours=1)).isoformat()


def insert(r, check_id=None, *, sent_at="2026-09-28T00:00:00Z"):
    seq = r.h.checkpoint(r.key) + 1
    fields = dict(identity(), event_id=str(uuid4()), source_seq=seq, acquisition_origin="signer",
                  change={"type": "message.upsert", "message": raw_message(str(uuid4()), sent_at=sent_at)})
    if check_id is not None:
        fields["check_id"] = check_id
    payload = message("ingest.delta", fields).payload
    return r.h.commit_delta(r.key, payload)


def test_canary_mismatch_by_insert(rig):
    check = canary(rig)
    assert insert(rig, check["check_id"]).status == "accepted"
    finish(rig, check, heads=[])
    assert state(rig)["status"] == "behind"


@pytest.mark.parametrize("attribution", ["absent", "unknown", "finished", "foreign", "newer"])
def test_concurrent_history_is_not_attributed(rig, attribution):
    r = rig
    prior = current(r)
    with r.h.database.transaction() as c:
        c.execute("UPDATE message_catchup SET next_canary_at=?", (START.isoformat(),))
    check = grant(r)
    identifier = {"absent": None, "unknown": str(uuid4()), "finished": prior["check_id"],
                  "foreign": str(uuid4()), "newer": check["check_id"]}[attribution]
    if attribution == "foreign":
        with r.h.database.transaction() as c:
            c.execute("INSERT INTO message_checks(check_id,creator_account_id,kind,epoch,granted_at,blind,lease_expires_at,state) VALUES (?,'other','canary',1,?,0,?,'active')",
                      (identifier, START.isoformat(), (START + timedelta(minutes=5)).isoformat()))
    sent = START.isoformat() if attribution == "newer" else "2026-09-28T00:00:00Z"
    assert insert(r, identifier, sent_at=sent).status == "accepted"
    finish(r, check, heads=[])
    assert state(r)["status"] == "current"


def test_completion_waits_for_final_source_sequence(rig):
    check = grant(rig)
    with pytest.raises(InvariantViolation):
        finish(rig, check, final=rig.h.checkpoint(rig.key) + 5)
    assert row(rig)["active_check_id"] == check["check_id"]
    assert state(rig)["status"] == "checking"


def test_completion_requires_inventory_and_reconciled_count(rig):
    r = rig
    check = grant(r)
    evidence(r, check, "check.inventory_closed", strategy="mixed", scanned=1, changed=1, movers=0)
    with pytest.raises(InvariantViolation):
        evidence(r, check, "check.completed", kind="catch_up", final_source_seq=r.h.checkpoint(r.key), pages_read=2, counts=dict(list=1, messages=1, probes=0))
    evidence(r, check, "check.chat_reconciled", chat_id="chat-1", target_head={}, reached="boundary", final_source_seq=r.h.checkpoint(r.key))
    evidence(r, check, "check.completed", kind="catch_up", final_source_seq=r.h.checkpoint(r.key), pages_read=2, counts=dict(list=1, messages=1, probes=0))
    assert state(r)["status"] == "current"


def test_snapshot_replay_cannot_move_completion_or_gap(rig):
    r = rig
    check = current(r)
    with r.h.database.read() as c:
        completed = c.execute("SELECT completed_at FROM message_checks WHERE check_id=?", (check["check_id"],)).fetchone()[0]
    r.clock.now += timedelta(seconds=10)
    r.l.epoch_changed(ACCOUNT_ID, now=r.clock.now)
    saved = row(r)
    snapshot = uuid4()
    payload = begin(snapshot, chunks=1, chats=0, messages=0, coverage=1)
    payload.through_seq = r.h.checkpoint(r.key)
    r.h.begin_snapshot(r.key, payload)
    r.h.add_snapshot_chunk(r.key, chunk(snapshot, 0, "coverage_evidence", [{
        "type": "check.completed", "generation_id": check["check_id"], "kind": "catch_up",
        "final_source_seq": payload.through_seq, "pages_read": 1,
        "counts": {"list": 1, "messages": 0, "probes": 0},
    }]))
    assert r.h.commit_snapshot(r.key, commit(snapshot, 1)).status == "accepted"
    assert row(r) == saved
    with r.h.database.read() as c:
        assert c.execute("SELECT completed_at FROM message_checks WHERE check_id=?", (check["check_id"],)).fetchone()[0] == completed


def test_old_delta_shape_stays_valid_and_check_id_is_accepted():
    old = delta(1, {"type": "message.upsert", "message": raw_message("sample")}).model_dump(mode="json", exclude_unset=True)
    assert "check_id" not in old
    old["check_id"] = str(uuid4())
    assert message("ingest.delta", old).payload.check_id is not None


@pytest.mark.asyncio
async def test_channel_rotation_24_hours_and_late_replacement(rig):
    from app.transport.manager import InMemoryTransportManager
    r = rig
    manager = InMemoryTransportManager(r.repos)
    manager.config_authority.bind_installation = lambda *args: SimpleNamespace(required_config_revision="config-history-1", applied_config_revision="config-history-1")
    manager.broadcast_agent_state = _noop
    socket = SimpleNamespace()
    async def admit():
        return await manager.bind_agent(socket, principal_id="test", creator_account_id=ACCOUNT_ID,
            agent_installation_id=INSTALLATION_ID, agent_stream_id=STREAM_ID,
            applied_config_revision="config-history-1", capabilities=["history.catchup.v1"], now=r.clock.now)
    lease = await admit()
    current(r)
    canaries = 0
    for minute in range(1, 1441):
        r.clock.now = START + timedelta(minutes=minute)
        r.l.heartbeat(ACCOUNT_ID, now=r.clock.now)
        lease.last_heartbeat_at = r.clock.now
        r.l.report(ACCOUNT_ID, report(r.clock.now, minute + 1), now=r.clock.now)
        if minute % 15 == 0:
            await manager.disconnect_agent(lease.connection_id)
            assert row(r)["observing"] == 1
            assert state(r)["status"] == "current"
            lease = await admit()
        result = r.l.begin(ACCOUNT_ID, request(), now=r.clock.now)
        if result["result"] == "granted":
            assert result["kind"] == "canary"
            finish(r, result, heads=[])
            canaries += 1
        assert row(r)["observing"] == 1 and not row(r)["gap_open"]
        assert state(r)["status"] == "current"
    assert canaries == 24
    await manager.disconnect_agent(lease.connection_id)
    last_heartbeat = r.clock.now
    r.clock.now += timedelta(seconds=61)
    lease = await admit()
    assert row(r)["gap_open"] == 1
    assert row(r)["uncertain_since"] == last_heartbeat.isoformat()


async def _noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("negotiated", [False, True])
async def test_old_strict_parser_and_negotiated_snapshot(rig, negotiated):
    from app.transport.manager import InMemoryTransportManager
    r = rig
    manager = InMemoryTransportManager(r.repos)
    documents = []
    async def send_text(raw):
        documents.append(json.loads(raw))
    socket = SimpleNamespace(send_text=send_text)
    await manager.bind_bridge(socket, principal_id="test", creator_account_id=ACCOUNT_ID,
        bridge_session_id=uuid4(), capabilities=["state.catchup_freshness"] if negotiated else [])
    await manager.send_bridge(socket, "state.snapshot", manager.state_snapshot_payload(ACCOUNT_ID))
    assert "correlation_id" in documents[-1] and documents[-1]["correlation_id"] is None
    payload = documents[-1]["payload"]
    old_fields = {"creator_account_id", "view_revision", "generated_at", "conversations", "analytics", "coverage", "projection", "live_freshness"}
    assert set(payload) == old_fields | ({"catchup_freshness"} if negotiated else set())
    assert payload["live_freshness"]["status"] != "current"
    await manager.send_bridge(socket, "state.delta", {
        "creator_account_id": ACCOUNT_ID, "view_revision": 1, "committed_at": START.isoformat(),
        "changes": [{"type": "live_freshness.replace", "live_freshness": r.h.live_freshness(ACCOUNT_ID)},
                    {"type": "catchup_freshness.replace", "catchup_freshness": state(r)}],
    })
    assert any(c["type"] == "catchup_freshness.replace" for c in documents[-1]["payload"]["changes"]) is negotiated


@pytest.mark.parametrize("supported", [True, False])
def test_legacy_freshness_mapping(rig, monkeypatch, supported):
    from app.transport.manager import InMemoryTransportManager
    r = rig
    r.l.admit(ACCOUNT_ID, installation=str(INSTALLATION_ID), stream=str(STREAM_ID), supported=supported, now=START)
    r.h.commit_delta(r.key, delta(1, {"type": "message.upsert", "message": raw_message("passive")}, origin="passive"))
    assert (r.h.live_freshness(ACCOUNT_ID, now=START)["status"] == "current") is (not supported)
    if not supported:
        assert state(r)["reason"] == "extension_outdated"
    manager = InMemoryTransportManager(r.repos)
    monkeypatch.setattr(manager.history, "coverage", lambda account: {"status": "complete"})
    monkeypatch.setattr(manager.projection, "state", lambda account: {
        "status": "current", "projected_revision": 1, "canonical_revision": 1,
    })
    assert (manager.system_state_payload(ACCOUNT_ID)["readiness"] == "ready") is (not supported)


@pytest.mark.parametrize("applied,requested,expected", [
    ("config-current", "config-current", "granted"),
    ("config-history-1", "config-history-1", "deferred"),
    ("config-current", "config-history-1", "deferred"),
])
def test_grant_requires_current_applied_configuration(rig, monkeypatch, applied, requested, expected):
    from app.protocol.config import HistoryCheckBeginRequest
    from app.transport.manager import InMemoryTransportManager
    r = rig
    manager = InMemoryTransportManager(r.repos)
    lease = SimpleNamespace(websocket=SimpleNamespace(), creator_account_id=ACCOUNT_ID,
                            applied_config_revision=applied)
    monkeypatch.setattr(manager, "_catchup_rpc_lease", lambda request: lease)
    monkeypatch.setattr(manager, "required_config_document",
                        lambda account: SimpleNamespace(config_revision="config-current"))
    body = dict(operation="history.check.begin", protocol_version="2", auth_ticket="fixture-ticket",
                agent_installation_id=str(INSTALLATION_ID), creator_account_id=ACCOUNT_ID,
                **request(config_revision=requested))
    result = manager.history_check_begin(HistoryCheckBeginRequest.model_validate_json(json.dumps(body)))
    assert result["result"] == expected
    if expected == "deferred":
        assert result["reason"] == "not_runnable"


@pytest.mark.parametrize("mutation,opens_gap", [("revoke", True), ("channel_close", False)])
def test_authorization_mutation_hook_excludes_channel_bookkeeping(rig, tmp_path, monkeypatch, mutation, opens_gap):
    from app.persistence.auth import SQLiteAuthenticationStore, RevocationKey, RevocationScopeType
    from app.transport.manager import InMemoryTransportManager, settings
    r = rig
    current(r)
    path = tmp_path / "authority.db"
    store = SQLiteAuthenticationStore(path, clock=lambda: r.clock.now)
    monkeypatch.setattr(settings, "auth_database_path", path)
    manager = InMemoryTransportManager(r.repos)
    r.clock.now += timedelta(seconds=5)
    if mutation == "revoke":
        store.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, "fixture-principal"))
    else:
        store.close_companion_session("fixture-session")
    assert bool(row(r)["gap_open"]) is opens_gap
    if opens_gap:
        assert row(r)["uncertain_since"] == r.clock.now.isoformat()
    assert manager.history is r.h


def test_late_replacement_uses_heartbeat_not_older_observation(rig):
    r = rig
    current(r)
    r.l.heartbeat(ACCOUNT_ID, now=START + timedelta(seconds=20))
    r.l.disconnect(ACCOUNT_ID, now=START + timedelta(seconds=20))
    r.clock.now = START + timedelta(seconds=81)
    r.l.admit(ACCOUNT_ID, installation=str(INSTALLATION_ID), stream=str(STREAM_ID), supported=True, now=r.clock.now)
    assert row(r)["uncertain_since"] == (START + timedelta(seconds=20)).isoformat()


def test_no_completed_generation_remains_never_checked(rig):
    r = rig
    with r.h.database.transaction() as c:
        c.execute("DELETE FROM message_catchup")
        c.execute("DELETE FROM coverage_generations")
        c.execute("UPDATE account_coverage_heads SET active_generation_id=NULL,last_complete_generation_id=NULL")
    r.l.admit(ACCOUNT_ID, installation=str(INSTALLATION_ID), stream=str(STREAM_ID), supported=True, now=START)
    r.l.report(ACCOUNT_ID, report(START, 2), now=START)
    assert state(r)["status"] == "never_checked"


@pytest.mark.windows_compat
def test_migration_seeds_latest_completed_generation(tmp_path):
    from pathlib import Path
    from app.persistence.database import CanonicalSQLite
    from app.persistence.history import HistoryRepository
    from app.persistence.migrations import MigrationRunner
    catalog = Path(__file__).parents[1] / "app/persistence/sql"
    previous = tmp_path / "previous"
    previous.mkdir()
    for source in sorted(catalog.glob("*.sql"))[:8]:
        (previous / source.name).write_bytes(source.read_bytes())
    db = CanonicalSQLite(tmp_path / "migration.db")
    MigrationRunner(db, migrations_dir=previous).run()
    with db.transaction() as c:
        for account in ("seeded", "empty"):
            HistoryRepository._ensure_account(c, account, START.isoformat())
        for days, status in ((3, "complete"), (2, "complete"), (1, "partial")):
            at = (START - timedelta(days=days)).isoformat()
            c.execute("INSERT INTO coverage_generations VALUES ('seeded',?,?,?, ?,?,?,NULL)",
                      (str(uuid4()), "consent", at, status, at, at))
    MigrationRunner(db).run()
    with db.read() as c:
        rows = c.execute("SELECT creator_account_id,gap_open,uncertain_since,last_closed_at FROM message_catchup ORDER BY creator_account_id").fetchall()
    assert tuple(rows[0]) == ("empty", 1, None, None)
    assert tuple(rows[1]) == ("seeded", 1, (START - timedelta(days=2)).isoformat(), (START - timedelta(days=2)).isoformat())
