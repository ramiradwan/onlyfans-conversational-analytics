"""Synchronous predecessor retirement batches high-volume page-cache cleanup."""
from pathlib import Path
import shutil

import pytest

from app.analytics.database import ProjectionsDatabase
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.opaque_refs import account_ref
from app.analytics.sqlite_projection_store import (
    SQLiteAnalyticsProjectionStore, _SCOPED_PAGE_RETIREMENT_RECLAIM,
)
from app.analytics.validation_receipt import content_stamp
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)

pytestmark = [pytest.mark.ci_tier("integration")]


def page_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("app.analytics.conversation_reuse.MAX_GRAPH_UNITS", 0)
    monkeypatch.setattr("app.analytics.conversation_reuse.MAX_ENRICHMENT_UNITS", 0)
    value = make_fixture(tmp_path, conversations=2, messages=0)
    with value.repositories.database.transaction() as db:
        for index in range(257):
            insert_message(db, "chat-0", f"retire-{index}", NOW, index)
    return value


def page_counts(connection, generation):
    return {
        "sets": connection.execute(
            "SELECT COUNT(*) FROM conversation_page_sets WHERE generation_id=?", (generation,)
        ).fetchone()[0],
        "owned": connection.execute(
            "SELECT COUNT(*) FROM conversation_owned_pages WHERE generation_id=?", (generation,)
        ).fetchone()[0],
        "refs": connection.execute(
            "SELECT COUNT(*) FROM conversation_page_refs WHERE generation_id=?", (generation,)
        ).fetchone()[0],
    }


def test_activation_batches_page_retirement_synchronously(tmp_path, monkeypatch):
    fixture = page_fixture(tmp_path, monkeypatch)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.read() as db:
            old = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            before = page_counts(db, old)
            old_content = {
                row[0] for row in db.execute(
                    "SELECT content_id FROM conversation_page_refs "
                    "WHERE generation_id=? ORDER BY content_id", (old,)
                )
            }
            assert before["sets"] > 0 and before["owned"] + before["refs"] > 0
            assert old_content
            assert content_stamp(db) is not None and _guards_match(db)
        with fixture.repositories.database.transaction() as db:
            insert_message(db, "chat-0", "retirement-next", NOW, 258)
            advance(db)
        result = fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.read() as db:
            assert db.execute(
                "SELECT status FROM projection_generations WHERE generation_id=?", (old,)
            ).fetchone()[0] == "retired"
            assert page_counts(db, old) == {"sets": 0, "owned": 0, "refs": 0}
            assert db.execute(
                "SELECT COUNT(*) FROM conversation_page_content c WHERE NOT EXISTS("
                "SELECT 1 FROM conversation_page_refs r "
                "WHERE r.creator_account_id=c.creator_account_id AND r.content_id=c.content_id)"
            ).fetchone()[0] == 0
            current = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            current_content = {
                row[0] for row in db.execute(
                    "SELECT content_id FROM conversation_page_refs "
                    "WHERE generation_id=? ORDER BY content_id", (current,)
                )
            }
            shared = old_content & current_content
            assert shared
            assert db.execute(
                "SELECT COUNT(*) FROM conversation_page_content "
                "WHERE creator_account_id=? AND content_id IN ("
                + ",".join("?" for _ in shared) + ")",
                (result.artifact.projection.account_ref, *sorted(shared)),
            ).fetchone()[0] == len(shared)
            assert db.execute(
                "SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()[0] == 0
            assert content_stamp(db) is not None and _guards_match(db)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        cold_equal(fixture, result.artifact)
    finally:
        cleanup(fixture)


def test_failed_batched_page_retirement_rolls_back(tmp_path, monkeypatch):
    fixture = page_fixture(tmp_path, monkeypatch)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        store = fixture.stores.projections
        with store.database.read() as db:
            old = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            before = page_counts(db, old)
        with pytest.raises(sqlite3.IntegrityError, match="injected page cleanup failure"):
            with store.database.transaction() as db:
                db.execute(
                    "CREATE TRIGGER fail_batched_page_cleanup BEFORE DELETE "
                    "ON conversation_page_content BEGIN "
                    "SELECT RAISE(ABORT,'injected page cleanup failure'); END"
                )
                store._retire_active_generation(
                    db, old, store._partition_ref(ACCOUNT), "2026-09-18T12:00:00.000000Z"
                )
        with store.database.read() as db:
            assert db.execute(
                "SELECT status FROM projection_generations WHERE generation_id=?", (old,)
            ).fetchone()[0] == "active"
            assert page_counts(db, old) == before
            assert db.execute(
                "SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()[0] == 0
            assert content_stamp(db) is not None and _guards_match(db)
    finally:
        cleanup(fixture)


def test_failed_retirement_after_page_batch_rolls_back(tmp_path, monkeypatch):
    fixture = page_fixture(tmp_path, monkeypatch)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        store = fixture.stores.projections
        with store.database.read() as db:
            old = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            before = page_counts(db, old)
            before_content = db.execute(
                "SELECT COUNT(*) FROM conversation_page_content"
            ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="injected retirement failure"):
            with store.database.transaction() as db:
                db.execute(
                    "CREATE TRIGGER fail_active_retirement BEFORE UPDATE OF status "
                    "ON projection_generations "
                    "WHEN OLD.status='active' AND NEW.status='retired' "
                    "BEGIN SELECT RAISE(ABORT,'injected retirement failure'); END"
                )
                store._retire_active_generation(
                    db, old, store._partition_ref(ACCOUNT), "2026-09-18T12:00:00.000000Z"
                )
        with store.database.read() as db:
            assert db.execute(
                "SELECT status FROM projection_generations WHERE generation_id=?", (old,)
            ).fetchone()[0] == "active"
            assert page_counts(db, old) == before
            assert db.execute(
                "SELECT COUNT(*) FROM conversation_page_content"
            ).fetchone()[0] == before_content
            assert db.execute(
                "SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()[0] == 0
            assert content_stamp(db) is not None and _guards_match(db)
    finally:
        cleanup(fixture)


def legacy_catalog(tmp_path):
    catalog = tmp_path / "legacy-catalog"
    catalog.mkdir()
    source = Path(__file__).parents[1] / "app/analytics/sql"
    for migration in sorted(source.glob("*.sql"))[:21]:
        shutil.copy2(migration, catalog / migration.name)
    return catalog


@pytest.mark.windows_compat
def test_schema22_upgrade_preserves_page_cache_and_backup(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path / "canonical", backend="memory")
    catalog = legacy_catalog(tmp_path)
    database = ProjectionsDatabase(tmp_path / "analytics.sqlite3", migrations_dir=catalog)
    options = dict(
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity,
    )
    monkeypatch.setattr("app.analytics.conversation_reuse.MAX_GRAPH_UNITS", 0)
    monkeypatch.setattr("app.analytics.conversation_reuse.MAX_ENRICHMENT_UNITS", 0)
    store = SQLiteAnalyticsProjectionStore(database, **options)
    pipeline = AnalyticsPipeline(
        fixture.source, projections=store, enrichment=fixture.pipeline.enrichment, clock=lambda: NOW
    )
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        with database.read() as db:
            before = db.execute("SELECT COUNT(*) FROM conversation_pages").fetchone()[0]
            assert before > 0 and db.execute("PRAGMA user_version").fetchone()[0] == 21
        upgraded = ProjectionsDatabase(database.path)
        with upgraded.read() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 25
            assert db.execute("SELECT COUNT(*) FROM conversation_pages").fetchone()[0] == before
            assert content_stamp(db) is not None and _guards_match(db)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        current = SQLiteAnalyticsProjectionStore(upgraded, **options)
        assert current.get_artifact(ACCOUNT) == expected
        backup = upgraded.migration_runner.last_backup_path
        assert backup is not None
        with upgraded.open_detached(backup, read_only=True) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 21
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='generation_content_bulk_cleanup'"
            ).fetchone() is None
    finally:
        cleanup(fixture)


@pytest.mark.windows_compat
def test_failed_schema22_migration_rolls_back(tmp_path):
    catalog = legacy_catalog(tmp_path)
    database = ProjectionsDatabase(tmp_path / "analytics.sqlite3", migrations_dir=catalog)
    source = Path(__file__).parents[1] / "app/analytics/sql"
    bad = tmp_path / "bad-catalog"
    bad.mkdir()
    for migration in sorted(source.glob("*.sql"))[:22]:
        shutil.copy2(migration, bad / migration.name)
    latest = bad / "0022_batched_page_retirement.sql"
    latest.write_text(latest.read_text() + "\nINVALID PAGE RETIREMENT MIGRATION;\n")
    with pytest.raises(sqlite3.DatabaseError):
        ProjectionsDatabase(database.path, migrations_dir=bad)
    with database.read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 21
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='generation_content_bulk_cleanup'"
        ).fetchone() is None
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_armed_bulk_cleanup_cannot_support_validation_receipts(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.transaction() as db:
            before = content_stamp(db)
            assert before is not None and _guards_match(db)
            db.execute(
                "UPDATE generation_content_bulk_cleanup SET page_retirement=1 WHERE singleton=1"
            )
            assert content_stamp(db) is None and not _guards_match(db)
            db.execute(
                "UPDATE generation_content_bulk_cleanup SET page_retirement=0 WHERE singleton=1"
            )
            after = content_stamp(db)
            assert after is not None and after != before and _guards_match(db)
    finally:
        cleanup(fixture)


def test_scoped_reclamation_plan_uses_retirement_ids(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        with fixture.stores.database.transaction() as db:
            db.execute(
                "CREATE TEMP TABLE retirement_page_content_ids ("
                "content_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            plan = [
                row[3] for row in db.execute(
                    "EXPLAIN QUERY PLAN " + _SCOPED_PAGE_RETIREMENT_RECLAIM,
                    (account_ref(ACCOUNT),),
                )
            ]
            assert any(
                "conversation_page_content" in step
                and "creator_account_id=?" in step
                and "content_id=?" in step
                for step in plan
            )
            assert any("retirement_page_content_ids" in step for step in plan)
            assert any(
                "conversation_page_refs" in step
                and "creator_account_id=?" in step
                and "content_id=?" in step
                for step in plan
            )
    finally:
        cleanup(fixture)
