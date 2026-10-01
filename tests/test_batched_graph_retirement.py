"""Synchronous predecessor retirement batches optional graph-unit cleanup."""
from pathlib import Path
import shutil

import pytest

from app.analytics.database import ProjectionsDatabase
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.analytics.validation_receipt import content_stamp
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)


def graph_state(db, generation):
    refs = {
        row[0] for row in db.execute(
            "SELECT unit_id FROM conversation_graph_refs "
            "WHERE generation_id=? ORDER BY unit_id", (generation,)
        )
    }
    return refs, db.execute(
        "SELECT COUNT(*) FROM conversation_graph_refs WHERE generation_id=?",
        (generation,),
    ).fetchone()[0]
def test_active_graph_reference_delete_is_blocked_without_batch(tmp_path):
    fixture = make_fixture(tmp_path, conversations=3, messages=5)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.transaction() as db:
            generation = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            row = db.execute(
                "SELECT creator_account_id,conversation_ref FROM conversation_graph_refs "
                "WHERE generation_id=? LIMIT 1", (generation,)
            ).fetchone()
            assert row is not None
            with pytest.raises(sqlite3.IntegrityError, match="conversation_graph_reference_delete_blocked"):
                db.execute(
                    "DELETE FROM conversation_graph_refs WHERE generation_id=? "
                    "AND creator_account_id=? AND conversation_ref=?",
                    (generation, row[0], row[1]),
                )
    finally:
        cleanup(fixture)


def test_graph_batch_reclaims_only_unshared_units(tmp_path):
    fixture = make_fixture(tmp_path, conversations=3, messages=5)
    try:
        first = fixture.pipeline.project_account(ACCOUNT).artifact
        with fixture.stores.database.read() as db:
            old = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            old_units, old_count = graph_state(db, old)
            assert old_count == 3 and old_units
        with fixture.repositories.database.transaction() as db:
            insert_message(db, "chat-0", "graph-retirement-next", NOW, 99)
            advance(db)
        result = fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.read() as db:
            new = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            new_units, new_count = graph_state(db, new)
            shared = old_units & new_units
            retired_only = old_units - new_units
            assert new_count == 3 and shared and retired_only
            assert graph_state(db, old)[1] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM conversation_graph_units "
                "WHERE unit_id IN (" + ",".join("?" for _ in shared) + ")",
                tuple(sorted(shared)),
            ).fetchone()[0] == len(shared)
            assert db.execute(
                "SELECT COUNT(*) FROM conversation_graph_units "
                "WHERE unit_id IN (" + ",".join("?" for _ in retired_only) + ")",
                tuple(sorted(retired_only)),
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT graph_retirement FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()[0] == 0
            assert content_stamp(db) is not None and _guards_match(db)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        cold_equal(fixture, result.artifact)
        assert first.projection.account_ref == result.artifact.projection.account_ref
    finally:
        cleanup(fixture)


def test_failed_retirement_rolls_back_graph_batch(tmp_path):
    fixture = make_fixture(tmp_path, conversations=3, messages=5)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        store = fixture.stores.projections
        with store.database.read() as db:
            old = db.execute(
                "SELECT generation_id FROM projection_generations WHERE status='active'"
            ).fetchone()[0]
            refs_before, count_before = graph_state(db, old)
            units_before = db.execute("SELECT COUNT(*) FROM conversation_graph_units").fetchone()[0]
            stamp_before = content_stamp(db)
        with pytest.raises(sqlite3.IntegrityError, match="injected retirement failure"):
            with store.database.transaction() as db:
                db.execute(
                    "CREATE TRIGGER fail_graph_batch_retirement BEFORE UPDATE OF status "
                    "ON projection_generations "
                    "WHEN OLD.status='active' AND NEW.status='retired' "
                    "BEGIN SELECT RAISE(ABORT,'injected retirement failure'); END"
                )
                store._retire_active_generation(
                    db, old, store._partition_ref(ACCOUNT),
                    "2026-09-18T12:00:00.000000Z",
                )
        with store.database.read() as db:
            assert db.execute(
                "SELECT status FROM projection_generations WHERE generation_id=?", (old,)
            ).fetchone()[0] == "active"
            refs_after, count_after = graph_state(db, old)
            assert refs_after == refs_before and count_after == count_before
            assert db.execute("SELECT COUNT(*) FROM conversation_graph_units").fetchone()[0] == units_before
            assert db.execute(
                "SELECT graph_retirement FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()[0] == 0
            assert content_stamp(db) == stamp_before and _guards_match(db)
    finally:
        cleanup(fixture)


def legacy22_catalog(tmp_path):
    catalog = tmp_path / "schema22-catalog"
    catalog.mkdir()
    source = Path(__file__).parents[1] / "app/analytics/sql"
    for migration in sorted(source.glob("*.sql"))[:22]:
        shutil.copy2(migration, catalog / migration.name)
    return catalog


def test_schema23_upgrade_preserves_published_graph_and_backup(tmp_path):
    fixture = make_fixture(tmp_path / "canonical", backend="memory", conversations=3, messages=5)
    catalog = legacy22_catalog(tmp_path)
    database = ProjectionsDatabase(tmp_path / "analytics.sqlite3", migrations_dir=catalog)
    options = dict(
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity,
    )
    store = SQLiteAnalyticsProjectionStore(database, **options)
    pipeline = AnalyticsPipeline(
        fixture.source, projections=store, enrichment=fixture.pipeline.enrichment, clock=lambda: NOW
    )
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        with database.read() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 22
            before = db.execute("SELECT COUNT(*) FROM conversation_graph_refs").fetchone()[0]
            assert before > 0
        upgraded = ProjectionsDatabase(database.path)
        with upgraded.read() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 25
            assert db.execute("SELECT COUNT(*) FROM conversation_graph_refs").fetchone()[0] == before
            assert tuple(db.execute(
                "SELECT page_retirement,graph_retirement "
                "FROM generation_content_bulk_cleanup WHERE singleton=1"
            ).fetchone()) == (0, 0)
            assert content_stamp(db) is not None and _guards_match(db)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        current = SQLiteAnalyticsProjectionStore(upgraded, **options)
        assert current.get_artifact(ACCOUNT) == expected
        backup = upgraded.migration_runner.last_backup_path
        assert backup is not None
        with upgraded.open_detached(backup, read_only=True) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 22
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(generation_content_bulk_cleanup)"
            )}
            assert "graph_retirement" not in columns
    finally:
        cleanup(fixture)


def test_armed_graph_cleanup_cannot_support_validation_receipts(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        with fixture.stores.database.transaction() as db:
            before = content_stamp(db)
            assert before is not None and _guards_match(db)
            db.execute(
                "UPDATE generation_content_bulk_cleanup SET graph_retirement=1 "
                "WHERE singleton=1 AND graph_retirement=0"
            )
            assert content_stamp(db) is None and not _guards_match(db)
            db.execute(
                "UPDATE generation_content_bulk_cleanup SET graph_retirement=0 "
                "WHERE singleton=1 AND graph_retirement=1"
            )
            after = content_stamp(db)
            assert after is not None and after != before and _guards_match(db)
    finally:
        cleanup(fixture)


def test_failed_schema23_migration_rolls_back(tmp_path):
    catalog = legacy22_catalog(tmp_path)
    database = ProjectionsDatabase(tmp_path / "analytics.sqlite3", migrations_dir=catalog)
    source = Path(__file__).parents[1] / "app/analytics/sql"
    bad = tmp_path / "bad-schema23-catalog"
    bad.mkdir()
    for migration in sorted(source.glob("*.sql"))[:23]:
        shutil.copy2(migration, bad / migration.name)
    latest = bad / "0023_batched_graph_retirement.sql"
    latest.write_text(latest.read_text() + "\nINVALID GRAPH RETIREMENT MIGRATION;\n")
    with pytest.raises(sqlite3.DatabaseError):
        ProjectionsDatabase(database.path, migrations_dir=bad)
    with database.read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 22
        columns = {
            row[1] for row in db.execute(
                "PRAGMA table_info(generation_content_bulk_cleanup)"
            )
        }
        assert "graph_retirement" not in columns
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
