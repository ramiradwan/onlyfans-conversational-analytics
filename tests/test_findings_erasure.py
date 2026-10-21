"""Synthetic findings across canonical deletion, durable cleanup and retries."""

from uuid import uuid4
from datetime import timedelta
from pathlib import Path
import json

import pytest

from app.analytics.nlp_erasure import FindingsErasure
from app.analytics.database import ProjectionsDatabase
from app.analytics.resilient_projection_store import LazySQLiteAnalyticsProjectionStore
from app.analytics.opaque_refs import account_ref
from app.persistence.deletion_operations import ManagedDeletionOperations
from app.persistence.retention import CreatorVaultRetention
from app.services.derived_deletion import DerivedDeletionService
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_nlp_processing_publication import fixture, runner

pytestmark = pytest.mark.ci_tier("integration")


def resources(f, **kwargs):
    erasure = FindingsErasure(f.source, f.stores.projections, clock=lambda: f.clock.now)
    operations = ManagedDeletionOperations(f.repositories.database)

    async def bridge(account):
        return f.repositories.projection.catch_up(account)

    service = DerivedDeletionService(
        operations, project_committed=bridge, reconcile_findings=erasure.step, **kwargs
    )
    return erasure, operations, service


def delete_all(f):
    operation = str(uuid4())
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_all(ACCOUNT, provenance="creator_delete:" + operation)
    return operation


async def drain(service, operation=None):
    for _ in range(40):
        result = await service.finish(ACCOUNT, operation)
        if operation and result.status == "complete":
            return result
        target = service.operations.target(ACCOUNT)
        with service.operations.database.read() as db:
            done = db.execute(
                "SELECT completed_revision FROM derived_deletion_work WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()
        if not operation and done and done[0] == target[0]:
            return None
    raise AssertionError("cleanup did not converge")


@pytest.mark.asyncio
async def test_account_erasure_requires_both_receipts_and_preserves_baseline(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    baseline_id = fixture.stores.database.active_generation(ACCOUNT).generation_id
    r, profile, _ = runner(fixture)
    assert r.process_one(ACCOUNT) and r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    with fixture.stores.database.read() as db:
        baseline = dict(
            db.execute(
                "SELECT * FROM projection_generations WHERE generation_id=?",
                (baseline_id,),
            ).fetchone()
        )
    erasure, operations, service = resources(fixture, max_steps=1)
    operation = delete_all(fixture)
    fixture.repositories.projection.catch_up(ACCOUNT)
    assert operations.get(ACCOUNT, operation).status != "complete"
    first = await service.finish(ACCOUNT, operation)
    assert first.status == "incomplete"
    assert r.findings.claim(ACCOUNT) is None
    with pytest.raises(ValueError, match="nlp_erasure_pending"):
        r.findings.select(ACCOUNT, profile)
    await drain(service, operation)
    assert operations.get(ACCOUNT, operation).status == "complete"
    with fixture.stores.database.read() as db:
        for table in (
            "nlp_accounts",
            "nlp_records",
            "nlp_jobs",
            "nlp_sources",
            "nlp_snapshots",
            "nlp_generation_refs",
            "nlp_record_bindings",
        ):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
        assert (
            db.execute("SELECT selection_epoch FROM nlp_epoch_ledger").fetchone()[0]
            >= 1
        )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert (
            dict(
                db.execute(
                    "SELECT * FROM projection_generations WHERE generation_id=?",
                    (baseline_id,),
                ).fetchone()
            )
            == baseline
        )
    r.findings.select(ACCOUNT, profile)
    assert r.findings.state(ACCOUNT)["selection_epoch"] > 1
    assert (await service.finish(ACCOUNT, operation)).status == "complete"


@pytest.mark.asyncio
async def test_scoped_cleanup_finds_historical_records_without_locators_and_keeps_valid_retained_binding(
    fixture, monkeypatch
):
    import app.analytics.nlp_erasure as module
    import app.analytics.nlp_processing as processing
    import app.analytics.nlp_findings_store as storage

    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    old_id = fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.read() as db:
        old_binding = tuple(
            db.execute(
                "SELECT * FROM nlp_generation_refs WHERE generation_id=?", (old_id,)
            ).fetchone()
        )
        first = db.execute(
            "SELECT source_ref FROM nlp_jobs WHERE state='done'"
        ).fetchone()[0]
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    with fixture.stores.database.read() as db:
        deleted = db.execute(
            "SELECT source_ref,message_id FROM nlp_sources WHERE source_ref!=?",
            (first,),
        ).fetchone()
    r.findings.invalidate_source(ACCOUNT, deleted["source_ref"])
    CreatorVaultRetention(
        fixture.repositories.database, clock=lambda: fixture.clock.now
    ).delete_message(ACCOUNT, deleted["message_id"])
    # Cleanup's canonical continuation is independent of analysis admission caps.
    monkeypatch.setattr(processing, "MAX_SOURCES", 1)
    monkeypatch.setattr(storage, "MAX_LOCATORS", 1)
    monkeypatch.setattr(module, "PAGE_SIZE", 1)
    _, operations, service = resources(fixture)
    await drain(service)
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_records").fetchone()[0] == 1
        assert (
            tuple(
                db.execute(
                    "SELECT * FROM nlp_generation_refs WHERE generation_id=?", (old_id,)
                ).fetchone()
            )
            == old_binding
        )
        assert (
            db.execute(
                "SELECT count(*) FROM nlp_jobs WHERE source_ref=? AND state='done'",
                (first,),
            ).fetchone()[0]
            == 1
        )
        assert not db.execute(
            "SELECT 1 FROM nlp_record_bindings WHERE source_ref=?",
            (deleted["source_ref"],),
        ).fetchone()
        assert (
            db.execute("SELECT phase FROM nlp_erasure_state").fetchone()[0]
            == "complete"
        )


@pytest.mark.asyncio
async def test_cleanup_replays_durable_receipt_after_canonical_ack_failure(
    fixture, monkeypatch
):
    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    _, operations, service = resources(fixture)
    operation = delete_all(fixture)
    original = operations.acknowledge

    def crash(*args, **kwargs):
        raise RuntimeError("simulated acknowledgement interruption")

    monkeypatch.setattr(operations, "acknowledge", crash)
    for _ in range(25):
        await service.finish(ACCOUNT, operation)
        with fixture.stores.database.read() as db:
            state = db.execute("SELECT phase,receipt FROM nlp_erasure_state").fetchone()
        if state and state["phase"] == "complete":
            break
    assert state["phase"] == "complete"
    assert operations.get(ACCOUNT, operation).status != "complete"
    reopened = ProjectionsDatabase(fixture.stores.database.path)
    with reopened.read() as db:
        assert (
            db.execute("SELECT receipt FROM nlp_erasure_state").fetchone()[0]
            == state["receipt"]
        )
    monkeypatch.setattr(operations, "acknowledge", original)
    _, fresh_operations, restarted = resources(fixture)
    assert (await restarted.finish(ACCOUNT, operation)).status == "complete"
    assert (await restarted.finish(ACCOUNT, operation)).status == "complete"
    with fixture.repositories.database.read() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM derived_deletion_acknowledgements WHERE operation_id=?",
                (operation,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_newer_barrier_cannot_be_acknowledged_by_old_receipt(fixture):
    _, operations, service = resources(fixture)
    first = delete_all(fixture)
    first_target = operations.target(ACCOUNT)[0]
    second = delete_all(fixture)
    assert operations.target(ACCOUNT)[0] > first_target
    assert not operations.acknowledge(ACCOUNT, first_target, "example", "receipt")
    await drain(service, second)
    assert operations.get(ACCOUNT, first).status == "complete"
    assert operations.get(ACCOUNT, second).status == "complete"
    with fixture.repositories.database.read() as db:
        rows = db.execute(
            "SELECT operation_id,creator_account_id,deletion_revision FROM derived_deletion_acknowledgements ORDER BY deletion_revision"
        ).fetchall()
        assert (
            len(rows) == 2
            and rows[0]["operation_id"] == first
            and rows[1]["operation_id"] == second
        )


def test_lifecycle_open_distinguishes_verified_absence_from_locked_storage(
    fixture, tmp_path
):
    options = dict(
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity,
        busy_timeout_ms=5,
    )
    missing = LazySQLiteAnalyticsProjectionStore(
        tmp_path / "missing.sqlite3", **options
    )
    with missing.lifecycle_store() as store:
        assert store is None
    assert not missing.path.exists()
    locked = LazySQLiteAnalyticsProjectionStore(fixture.stores.database.path, **options)
    identity = locked.path.stat().st_ino
    with fixture.stores.database.transaction():
        with locked.lifecycle_store() as store:
            # WAL reads may open successfully, but cannot acknowledge empty
            # state or begin erasure ahead of an uncommitted writer.
            with pytest.raises(Exception):
                FindingsErasure(
                    fixture.source, store, clock=lambda: fixture.clock.now
                ).step(ACCOUNT, 1, account_wide=1)
    assert locked.path.exists() and locked.path.stat().st_ino == identity
    with locked.lifecycle_store() as store:
        assert store is not None
    locked.close()


@pytest.mark.asyncio
async def test_retention_barriers_resume_without_new_notifications(fixture):
    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    fixture.clock.now += timedelta(days=100)
    assert CreatorVaultRetention(
        fixture.repositories.database, clock=lambda: fixture.clock.now
    ).enforce(ACCOUNT)
    _, operations, service = resources(fixture)
    for _ in range(30):
        await service.run_pending()
        with fixture.repositories.database.read() as db:
            row = db.execute(
                "SELECT target_revision,completed_revision FROM derived_deletion_work WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()
        if row["target_revision"] == row["completed_revision"]:
            break
    assert row["target_revision"] == row["completed_revision"]
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_records").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_lag", [False, True])
@pytest.mark.parametrize("initial_payload", [False, True])
async def test_completed_account_erase_does_not_erase_later_import_on_scoped_deletion(
    fixture, monkeypatch, ack_lag, initial_payload
):
    if initial_payload:
        r, profile, _ = runner(fixture)
        assert r.process_one(ACCOUNT)
        assert r.publish_pending(ACCOUNT).changed
    erasure, operations, service = resources(fixture)
    operation = delete_all(fixture)
    acknowledge = operations.acknowledge
    if ack_lag:
        monkeypatch.setattr(
            operations,
            "acknowledge",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated commit interruption")
            ),
        )
        for _ in range(5):
            await service.finish(ACCOUNT, operation)
            with fixture.stores.database.read() as db:
                row = db.execute("SELECT phase FROM nlp_erasure_state").fetchone()
            if row and row[0] == "complete":
                break
        assert (
            row[0] == "complete"
            and operations.get(ACCOUNT, operation).status != "complete"
        )
    else:
        await drain(service, operation)
    fixture.clock.now += timedelta(seconds=1)
    retention = CreatorVaultRetention(
        fixture.repositories.database, clock=lambda: fixture.clock.now
    )
    retention.set_policy(
        ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable-import"
    )
    for n in range(2):
        retention.import_message(
            ACCOUNT,
            action_ref="new-import-" + str(n),
            conversation={
                "chat_id": "import-chat",
                "platform_user_id": "import-participant",
            },
            message={
                "message_id": "new-" + str(n),
                "chat_id": "import-chat",
                "sender_platform_user_id": "import-participant",
                "text": "Thanks pricing",
                "sent_at": fixture.clock.now.isoformat(),
                "direction": "inbound",
            },
        )
    if not initial_payload:
        r, profile, _ = runner(fixture)
    else:
        r.findings.select(ACCOUNT, profile)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    retained = fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.read() as db:
        keep = db.execute(
            "SELECT source_ref FROM nlp_jobs WHERE state='done'"
        ).fetchone()[0]
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    before = r.findings.state(ACCOUNT)
    with fixture.stores.database.read() as db:
        doomed = db.execute(
            "SELECT message_id FROM nlp_sources WHERE source_ref!=?", (keep,)
        ).fetchone()[0]
        binding = tuple(
            db.execute(
                "SELECT * FROM nlp_generation_refs WHERE generation_id=?", (retained,)
            ).fetchone()
        )
    fixture.clock.now += timedelta(seconds=1)
    retention.delete_message(ACCOUNT, doomed)
    if ack_lag:
        monkeypatch.setattr(operations, "acknowledge", acknowledge)
        assert operations.target(ACCOUNT)[1] == 1  # Canonical receipt still lags.
    await drain(service)
    after = r.findings.state(ACCOUNT)
    assert (
        after["profile_json"] == before["profile_json"]
        and after["selection_epoch"] == before["selection_epoch"]
    )
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_records").fetchone()[0] == 1
        assert (
            tuple(
                db.execute(
                    "SELECT * FROM nlp_generation_refs WHERE generation_id=?",
                    (retained,),
                ).fetchone()
            )
            == binding
        )
        assert (
            db.execute(
                "SELECT count(*) FROM nlp_jobs WHERE source_ref=?", (keep,)
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT covered_account_revision FROM nlp_erasure_state"
            ).fetchone()[0]
            == 1
        )
    assert operations.get(ACCOUNT, operation).status == "complete"


def test_expired_service_budget_stops_before_calling_cleanup(fixture):
    operation = delete_all(fixture)
    _, operations, service = resources(fixture, budget_seconds=0)
    calls = []
    service.reconcile_findings = lambda *args, **kwargs: calls.append(True)
    assert not service.reconcile(ACCOUNT) and not calls
    assert operations.get(ACCOUNT, operation).status != "complete"


@pytest.mark.asyncio
async def test_tiny_account_erasure_completes_in_one_bounded_service_pass(fixture):
    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT) and r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    _, operations, service = resources(fixture)
    operation = delete_all(fixture)
    result = await service.finish(ACCOUNT, operation)
    assert result.status == "complete"


@pytest.mark.asyncio
async def test_bridge_failure_does_not_lose_completed_analytics_receipt(fixture):
    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    _, operations, service = resources(fixture)
    operation = delete_all(fixture)
    bridge = service.project_committed

    async def unavailable(account):
        raise RuntimeError("simulated bridge interruption")

    service.project_committed = unavailable
    result = await service.finish(ACCOUNT, operation)
    assert result.status == "incomplete"
    with fixture.stores.database.read() as db:
        receipt = db.execute("SELECT receipt FROM nlp_erasure_state").fetchone()[0]
    assert receipt
    service.project_committed = bridge
    assert (await service.finish(ACCOUNT, operation)).status == "complete"
    with fixture.stores.database.read() as db:
        assert (
            db.execute("SELECT receipt FROM nlp_erasure_state").fetchone()[0] == receipt
        )


@pytest.mark.asyncio
async def test_maintenance_timeout_retains_worker_then_drains_without_replay(
    monkeypatch,
):
    import asyncio
    import threading
    import app.services.retention_maintenance as maintenance

    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked():
        calls.append(True)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(maintenance, "_DEFAULT_INFLIGHT", set())
    task = asyncio.create_task(maintenance._owned(asyncio.to_thread(blocked)))
    monkeypatch.setattr(maintenance, "_DEFAULT_TASK", task)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not await maintenance.shutdown_default_retention_maintenance(
            timeout=0.01
        )
        assert maintenance._DEFAULT_INFLIGHT
        with pytest.raises(RuntimeError, match="retention_maintenance_not_drained"):
            await maintenance.start_default_retention_maintenance()
        assert calls == [True]
    finally:
        release.set()
        assert await maintenance.shutdown_default_retention_maintenance(timeout=2)
    assert not maintenance._DEFAULT_INFLIGHT and calls == [True]


@pytest.mark.asyncio
async def test_main_does_not_close_dependencies_while_maintenance_survives(monkeypatch):
    import app.main as main
    import app.services.retention_maintenance as maintenance

    calls = []

    async def not_drained(**kwargs):
        return False

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("dependency closed under surviving work")

    monkeypatch.setattr(
        maintenance, "shutdown_default_retention_maintenance", not_drained
    )
    monkeypatch.setattr(main.transport_manager, "stop", forbidden)
    monkeypatch.setattr(
        main.analytics_runtime, "shutdown_default_analytics_runtime", forbidden
    )
    await main.shutdown_event()
    assert not main.app.state.runtime_shutdown_drained and not calls


def test_corrupt_store_is_not_treated_as_absent_or_recreated(fixture, tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    raw = b"invented unreadable encrypted storage"
    path.write_bytes(raw)
    lazy = LazySQLiteAnalyticsProjectionStore(
        path,
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity,
        busy_timeout_ms=5,
    )
    with pytest.raises(Exception):
        with lazy.lifecycle_store():
            pytest.fail("corrupt storage was admitted")
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob("*quarantine*"))
    lazy.close()


def test_evidence_import_admission_keeps_ordinary_and_newer_deletion_barriers(fixture):
    from app.analytics.evidence_contracts import EvidenceLocation
    from app.analytics.query_execution import QuestionBudget, QuestionLimits

    retention = CreatorVaultRetention(
        fixture.repositories.database, clock=lambda: fixture.clock.now
    )
    retention.delete_all(ACCOUNT)
    retention.set_policy(
        ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable"
    )
    fixture.clock.now += timedelta(seconds=1)

    def import_message(action):
        retention.import_message(
            ACCOUNT,
            action_ref=action,
            conversation={
                "chat_id": "import-chat",
                "platform_user_id": "import-participant",
            },
            message={
                "message_id": "import-message",
                "chat_id": "import-chat",
                "sender_platform_user_id": "import-participant",
                "text": "Thanks pricing",
                "sent_at": fixture.clock.now.isoformat(),
                "direction": "inbound",
            },
        )

    def read():
        return fixture.source.read_evidence_message(
            ACCOUNT,
            EvidenceLocation(
                conversation_id="import-chat", message_id="import-message"
            ),
            QuestionBudget(QuestionLimits(max_records=8, wall_clock_ms=1000)),
        )

    import_message("first")
    assert read() is not None
    with fixture.repositories.database.read() as db:
        original = dict(
            db.execute(
                "SELECT * FROM account_messages WHERE message_id='import-message'"
            ).fetchone()
        )
    fixture.clock.now += timedelta(seconds=1)
    retention.delete_message(ACCOUNT, "import-message")
    with fixture.repositories.database.transaction() as db:
        ordinary = dict(original, lifecycle_origin="ordinary")
        names = ",".join(ordinary)
        marks = ",".join("?" for _ in ordinary)
        db.execute(
            "INSERT INTO account_messages(" + names + ") VALUES(" + marks + ")",
            tuple(ordinary.values()),
        )
        assert not db.execute(
            "SELECT 1 FROM account_messages WHERE message_id='import-message'"
        ).fetchone()
        db.execute(
            "INSERT INTO account_messages(" + names + ") VALUES(" + marks + ")",
            tuple(original.values()),
        )
    assert read() is None  # A restored older import does not erase the newer barrier.
    with fixture.repositories.database.transaction() as db:
        db.execute("DELETE FROM account_messages WHERE message_id='import-message'")
    fixture.clock.now += timedelta(seconds=1)
    import_message("second")
    assert read() is not None


@pytest.mark.asyncio
async def test_pending_erasure_fences_late_completion_and_generation_publication(
    fixture,
):
    from app.analytics.evidence_contracts import EvidenceLocation
    from tests.test_nlp_record_validation import case, frame

    r, _, _ = runner(fixture)
    r.reconcile(ACCOUNT)
    claim = r.findings.claim(ACCOUNT)
    source = r._read(
        ACCOUNT,
        EvidenceLocation(
            conversation_id=claim.conversation_id, message_id=claim.message_id
        ),
        r._budget(),
    )
    profile, _, document = case(message=source)
    pipeline = r.frozen_pipeline(ACCOUNT)
    candidate = pipeline.build_candidate(ACCOUNT)
    erasure, operations, service = resources(fixture, max_steps=1)
    operation = delete_all(fixture)
    assert not service.reconcile(ACCOUNT)
    assert not r.findings.valid_claim(claim)
    with pytest.raises(ValueError, match="nlp_claim_stale"):
        r.findings.complete(claim, profile, source, frame(document))
    with pytest.raises(ValueError, match="nlp_erasure_pending"):
        pipeline.publish_candidate(candidate)
    await drain(service, operation)
    r.findings.select(ACCOUNT, profile)
    assert r.findings.state(ACCOUNT)["selection_epoch"] > claim.selection_epoch
    assert not r.findings.valid_claim(claim)


@pytest.mark.asyncio
async def test_corrupt_ownership_cannot_satisfy_deletion_acknowledgement(fixture):
    r, _, _ = runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    with fixture.stores.database.transaction() as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='nlp_members_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER nlp_members_delete")
        db.execute("DELETE FROM nlp_snapshot_members")
        db.execute(sql)
    _, operations, service = resources(fixture)
    operation = delete_all(fixture)
    assert (await service.finish(ACCOUNT, operation)).status == "incomplete"
    assert operations.get(ACCOUNT, operation).status != "complete"
    with fixture.repositories.database.read() as db:
        assert not db.execute(
            "SELECT 1 FROM derived_deletion_acknowledgements WHERE operation_id=?",
            (operation,),
        ).fetchone()
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_records").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM nlp_generation_refs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancelled_service_request_keeps_cleanup_owned(fixture, monkeypatch):
    import asyncio
    import threading
    import app.services.retention_maintenance as maintenance

    _, _, service = resources(fixture)
    started, release = threading.Event(), threading.Event()
    calls = []

    def reconcile(account):
        calls.append(account)
        started.set()
        assert release.wait(5)
        return False

    monkeypatch.setattr(service, "reconcile", reconcile)
    monkeypatch.setattr(maintenance, "_DEFAULT_INFLIGHT", set())
    monkeypatch.setattr(maintenance, "_DEFAULT_TASK", None)
    request = asyncio.create_task(service.finish(ACCOUNT))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not await maintenance.shutdown_default_retention_maintenance(
            timeout=0.01
        )
        assert maintenance._DEFAULT_INFLIGHT
    finally:
        release.set()
        assert await maintenance.shutdown_default_retention_maintenance(timeout=2)
    assert calls == [ACCOUNT]


def test_absence_acknowledgement_serializes_new_store_creation(
    fixture, tmp_path, monkeypatch
):
    import threading
    from types import SimpleNamespace
    import app.analytics.runtime as runtime

    lazy = LazySQLiteAnalyticsProjectionStore(
        tmp_path / "absent.sqlite3",
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity,
    )
    monkeypatch.setattr(
        runtime, "_DEFAULT_CONFIGURATION", SimpleNamespace(backend="sqlite")
    )
    monkeypatch.setattr(
        runtime,
        "analytics_runtime",
        lambda: SimpleNamespace(
            pipeline=SimpleNamespace(projections=lazy), source=fixture.source
        ),
    )
    operation = delete_all(fixture)
    operations = ManagedDeletionOperations(fixture.repositories.database)
    revision, wide = operations.target(ACCOUNT)
    attempted, created = threading.Event(), threading.Event()
    threads = []

    def create():
        attempted.set()
        lazy.ensure_ready()
        created.set()

    def acknowledge(receipt):
        thread = threading.Thread(target=create)
        threads.append(thread)
        thread.start()
        assert attempted.wait(2)
        assert not created.wait(0.05)
        assert operations.acknowledge(ACCOUNT, revision, "absent", json.dumps(receipt))

    try:
        receipt = runtime.reconcile_findings_erasure(
            ACCOUNT, revision, account_wide=wide, acknowledge_absence=acknowledge
        )
        assert receipt["store_identity"] == "absent"
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert created.is_set()
        assert operations.target(ACCOUNT)[1] == 0
        with fixture.repositories.database.read() as db:
            assert db.execute(
                "SELECT 1 FROM derived_deletion_acknowledgements WHERE operation_id=?",
                (operation,),
            ).fetchone()
    finally:
        lazy.close()


def test_absence_receipt_cannot_acknowledge_newer_target(fixture):
    _, operations, _ = resources(fixture)
    delete_all(fixture)

    def absence(account, revision, *, account_wide, deadline, acknowledge_absence):
        delete_all(fixture)
        receipt = {
            "account_ref": account_ref(account),
            "target_revision": revision,
            "store_identity": "absent",
            "account_revision": account_wide,
        }
        assert not acknowledge_absence(receipt)
        return receipt

    service = DerivedDeletionService(
        operations, project_committed=None, reconcile_findings=absence
    )
    assert not service.reconcile(ACCOUNT)
    with fixture.repositories.database.read() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM derived_deletion_acknowledgements"
            ).fetchone()[0]
            == 0
        )
        row = db.execute("SELECT * FROM derived_deletion_work").fetchone()
        assert row["completed_revision"] < row["target_revision"]
