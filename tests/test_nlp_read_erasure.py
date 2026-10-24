"""Historical pending-source membership participates in exact scoped erasure."""

from datetime import timedelta
import json

import pytest

from app.analytics.nlp_erasure import FindingsErasure
from app.analytics.opaque_refs import account_ref
from app.persistence.retention import CreatorVaultRetention
from tests.continuous_analytics_fixture import (
    ACCOUNT,
    make_fixture,
    cleanup,
    insert_message,
    advance,
)
from tests.test_findings_erasure import resources, drain, delete_all
from tests.test_nlp_processing_publication import runner

pytestmark = pytest.mark.ci_tier("integration")


@pytest.fixture
def history(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=1)
    f.stores.projections.rollback_retention = 8
    r, _, _ = runner(f)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    old = f.stores.database.active_generation(ACCOUNT).generation_id
    with f.repositories.database.transaction() as db:
        insert_message(db, "chat-0", "later-pending", f.clock.now - timedelta(days=1))
        advance(db)
    r.reconcile(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    newer = f.stores.database.active_generation(ACCOUNT).generation_id
    with f.stores.database.read() as db:
        bindings = {
            row["generation_id"]: row["snapshot_id"]
            for row in db.execute("SELECT * FROM nlp_generation_refs")
        }
        original = bytes(
            db.execute(
                "SELECT document FROM nlp_snapshots WHERE snapshot_id=?",
                (bindings[old],),
            ).fetchone()[0]
        )
        assert json.loads(original)["read_inventory"]["source_count"] == 1
        assert (
            json.loads(
                db.execute(
                    "SELECT document FROM nlp_snapshots WHERE snapshot_id=?",
                    (bindings[newer],),
                ).fetchone()[0]
            )["read_inventory"]["source_count"]
            == 2
        )
    try:
        yield f, old, newer, bindings, original
    finally:
        cleanup(f)


@pytest.mark.asyncio
async def test_pending_only_deletion_retires_exact_newer_snapshot_and_keeps_older(
    history,
):
    f, old, newer, bindings, original = history
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_message(ACCOUNT, "later-pending")
    _, _, service = resources(f, max_steps=1)
    # Every call reopens durable progress rather than relying on an in-memory scan.
    await drain(service)
    with f.stores.database.read() as db:
        assert db.execute(
            "SELECT 1 FROM nlp_generation_refs WHERE generation_id=?", (old,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM nlp_generation_refs WHERE generation_id=?", (newer,)
        ).fetchone()
        assert (
            bytes(
                db.execute(
                    "SELECT document FROM nlp_snapshots WHERE snapshot_id=?",
                    (bindings[old],),
                ).fetchone()[0]
            )
            == original
        )
        assert not db.execute(
            "SELECT 1 FROM nlp_snapshot_source_pages WHERE snapshot_id=?",
            (bindings[newer],),
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM nlp_snapshot_source_pages WHERE snapshot_id=?",
            (bindings[old],),
        ).fetchone()
    operation = delete_all(f)
    await drain(service, operation)
    with f.stores.database.read() as db:
        assert (
            db.execute("SELECT count(*) FROM nlp_snapshot_source_pages").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("fault", ["missing", "tampered"])
def test_bad_page_never_acknowledges_erasure(history, fault):
    f, _, _, bindings, _ = history
    with f.stores.database.transaction() as db:
        name = (
            "nlp_source_page_delete"
            if fault == "missing"
            else "nlp_source_page_immutable"
        )
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()[0]
        db.execute("DROP TRIGGER " + name)
        if fault == "missing":
            db.execute(
                "DELETE FROM nlp_snapshot_source_pages WHERE snapshot_id=?",
                (next(iter(bindings.values())),),
            )
        else:
            raw = bytes(
                db.execute(
                    "SELECT document FROM nlp_snapshot_source_pages LIMIT 1"
                ).fetchone()[0]
            )
            db.execute(
                "UPDATE nlp_snapshot_source_pages SET document=?",
                (raw.replace(b"chat-0", b"chat-1"),),
            )
        db.execute(sql)
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_message(ACCOUNT, "later-pending")
    erasure, operations, _ = resources(f)
    revision, wide = operations.target(ACCOUNT)
    with pytest.raises(ValueError, match="nlp_read_page_invalid"):
        for _ in range(20):
            erasure.step(ACCOUNT, revision, account_wide=wide)
    assert erasure._state(account_ref(ACCOUNT))["phase"] != "complete"


def test_page_scan_source_change_cannot_commit_progress(history, monkeypatch):
    f, _, _, _, _ = history
    erasure, operations, _ = resources(f)
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_message(ACCOUNT, "later-pending")
    revision, wide = operations.target(ACCOUNT)
    while (state := erasure._state(account_ref(ACCOUNT))) is None or state[
        "phase"
    ] != "erase":
        erasure.step(ACCOUNT, revision, account_wide=wide)
    original = f.source.read_evidence_message

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with f.repositories.database.transaction() as db:
            advance(db)
        return result

    monkeypatch.setattr(f.source, "read_evidence_message", changed)
    with pytest.raises(ValueError, match="nlp_erasure_source_changed"):
        erasure._source_page_step(ACCOUNT, state, __import__("time").monotonic() + 5)
    with f.stores.database.read() as db:
        assert not db.execute(
            "SELECT 1 FROM nlp_snapshot_source_pages WHERE erasure_seen_epoch=?",
            (state["scan_epoch"],),
        ).fetchone()


@pytest.mark.parametrize("change", ["stamp", "state"])
def test_page_scan_compares_content_stamp_and_exact_erasure_state(
    history, monkeypatch, change
):
    import time

    f, _, _, _, _ = history
    erasure, operations, _ = resources(f)
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_message(ACCOUNT, "later-pending")
    revision, wide = operations.target(ACCOUNT)
    while (state := erasure._state(account_ref(ACCOUNT))) is None or state[
        "phase"
    ] != "erase":
        erasure.step(ACCOUNT, revision, account_wide=wide)
    original = f.source.read_evidence_message
    changed = False

    def interleave(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            with f.stores.database.transaction() as db:
                if change == "stamp":
                    db.execute("UPDATE generation_content_epoch SET value=value+1")
                else:
                    db.execute(
                        "UPDATE nlp_erasure_state SET target_revision=target_revision+1"
                    )
        return result

    monkeypatch.setattr(f.source, "read_evidence_message", interleave)
    with pytest.raises(ValueError, match="nlp_erasure_retry"):
        erasure._source_page_step(ACCOUNT, state, time.monotonic() + 5)
    with f.stores.database.read() as db:
        assert not db.execute(
            "SELECT 1 FROM nlp_snapshot_source_pages WHERE erasure_seen_epoch=?",
            (state["scan_epoch"],),
        ).fetchone()


def test_page_progress_survives_restart_but_retention_due_resets_scan(
    history, monkeypatch
):
    import time

    f, _, _, _, _ = history
    erasure, operations, _ = resources(f)
    CreatorVaultRetention(
        f.repositories.database, clock=lambda: f.clock.now
    ).delete_message(ACCOUNT, "later-pending")
    revision, wide = operations.target(ACCOUNT)
    while (state := erasure._state(account_ref(ACCOUNT))) is None or state[
        "phase"
    ] != "erase":
        erasure.step(ACCOUNT, revision, account_wide=wide)
    assert erasure._source_page_step(ACCOUNT, state, time.monotonic() + 5)
    restarted = FindingsErasure(
        f.source, f.stores.projections, clock=lambda: f.clock.now
    )
    persisted = restarted._state(account_ref(ACCOUNT))
    assert persisted["due_at"]
    f.clock.now += timedelta(days=100)

    def unexpected_erase(*args, **kwargs):
        pytest.fail("derived deletion preceded the required retention rescan")

    monkeypatch.setattr("app.analytics.nlp_erasure.erase_page", unexpected_erase)
    assert restarted._erase(ACCOUNT, persisted, time.monotonic() + 5) is None
    assert restarted._state(account_ref(ACCOUNT))["phase"] == "scan"
