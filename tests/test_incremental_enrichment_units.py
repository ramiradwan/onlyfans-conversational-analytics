"""Qualify immutable conversation enrichment units used by incremental projections."""

import json

import pytest

from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cold_equal, insert_message, make_fixture, cleanup,
)
from tests.test_shared_graph import fixture


def counts(fixture):
    with fixture.stores.database.read() as db:
        return {
            name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in (
                "conversation_enrichment_units",
                "conversation_enrichment_refs",
            )
        }


def active_units(fixture):
    with fixture.stores.database.read() as db:
        generation = db.execute(
            "SELECT generation_id FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        rows = db.execute(
            """SELECT r.conversation_ref,r.ordinal,r.unit_id,u.message_count,
                      u.metrics_json,u.canonical_digest
               FROM conversation_enrichment_refs r
               JOIN conversation_enrichment_units u
                 USING(creator_account_id,unit_id)
               WHERE r.generation_id=? ORDER BY r.ordinal""",
            (generation,),
        )
        return [dict(row) for row in rows]


def test_cold_build_persists_complete_enrichment_units_and_compact_document(fixture):
    result = fixture.pipeline.project_account(ACCOUNT)
    state = counts(fixture)
    metrics = result.artifact.projection.conversation_metrics
    assert state["conversation_enrichment_units"] == len(metrics)
    assert state["conversation_enrichment_refs"] == len(metrics)

    rows = active_units(fixture)
    assert [row["ordinal"] for row in rows] == list(range(len(rows)))
    assert sum(row["message_count"] for row in rows) == len(
        result.artifact.projection.message_enrichments
    )
    assert [json.loads(row["metrics_json"]) for row in rows] == [
        item.model_dump(mode="json") for item in metrics
    ]

    with fixture.stores.database.read() as db:
        assert db.execute(
            """SELECT COUNT(*) FROM enrichment_refs r
               JOIN projection_generations g USING(generation_id,creator_account_id)
               WHERE g.status='active'"""
        ).fetchone()[0] == 0
        assert db.execute(
            """SELECT COUNT(*) FROM enrichment_owned_records r
               JOIN projection_generations g USING(generation_id,creator_account_id)
               WHERE g.status='active'"""
        ).fetchone()[0] == 0
        document = db.execute(
            """SELECT p.document_json FROM analytics_projections p
               JOIN projection_generations g
                 USING(generation_id,creator_account_id)
               WHERE g.status='active'"""
        ).fetchone()[0]
    assert json.loads(document)["message_enrichments"] == []
    assert fixture.stores.projections.get(ACCOUNT) == result.artifact.projection


def test_unchanged_generation_reuses_enrichment_unit_content(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    previous = {
        row["conversation_ref"]: row["unit_id"] for row in active_units(fixture)
    }
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    during = counts(fixture)
    assert during["conversation_enrichment_units"] == before["conversation_enrichment_units"]
    assert during["conversation_enrichment_refs"] == before["conversation_enrichment_refs"] * 2
    fixture.pipeline.publish_candidate(candidate)
    assert {
        row["conversation_ref"]: row["unit_id"] for row in active_units(fixture)
    } == previous


def test_one_changed_conversation_replaces_only_its_enrichment_unit(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    previous = {
        row["conversation_ref"]: row["unit_id"] for row in active_units(fixture)
    }
    before_calls = sum(analyzer.calls for analyzer in fixture.analyzers)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, "chat-1", "new enrichment-unit message", NOW)
        advance(db)
    result = fixture.pipeline.project_account(ACCOUNT)
    current = {
        row["conversation_ref"]: row["unit_id"] for row in active_units(fixture)
    }
    changed = [ref for ref in current if current[ref] != previous[ref]]
    assert len(changed) == 1
    assert sum(analyzer.calls for analyzer in fixture.analyzers) - before_calls == 3
    cold_equal(fixture, result.artifact)


def test_conversation_unit_cache_reuses_analyzers_after_account_cache_limit(
    tmp_path, monkeypatch
):
    import app.analytics.enrichment_cache as cache

    monkeypatch.setattr(cache, "MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 4096)
    value = make_fixture(tmp_path, conversations=2, messages=130)
    try:
        value.pipeline.project_account(ACCOUNT)
        before = sum(analyzer.calls for analyzer in value.analyzers)
        with value.repositories.database.transaction() as db:
            insert_message(db, "chat-1", "cache-limit-unit-message", NOW)
            advance(db)
        result = value.pipeline.project_account(ACCOUNT)
        assert sum(analyzer.calls for analyzer in value.analyzers) - before == 3
        cold_equal(value, result.artifact)
    finally:
        cleanup(value)


def test_incremental_validation_checks_only_changed_enrichment_unit(
    fixture, monkeypatch
):
    import app.analytics.conversation_enrichment_unit_sql as storage

    fixture.pipeline.project_account(ACCOUNT)
    calls = []
    original = storage._validate_unit

    def observed(unit, **kwargs):
        calls.append(unit.header.conversation_ref)
        return original(unit, **kwargs)

    monkeypatch.setattr(storage, "_validate_unit", observed)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, "chat-1", "validate-one-enrichment-unit", NOW)
        advance(db)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert len(calls) == 1
    fixture.pipeline.publish_candidate(candidate)


def test_missing_process_proof_falls_back_without_streamed_enrichment_reuse(
    fixture, monkeypatch
):
    import app.analytics.conversation_enrichment_units as units

    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._conversation_enrichment_proofs.clear()

    class Forbidden(units.IncrementalMessageEnrichments):
        def __init__(self, *args, **kwargs):
            raise AssertionError("streamed enrichment reuse required a missing proof")

    monkeypatch.setattr(units, "IncrementalMessageEnrichments", Forbidden)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, "chat-1", "missing-proof-message", NOW)
        advance(db)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result.changed


@pytest.mark.parametrize(
    "table,column",
    [
        ("conversation_enrichment_units", "canonical_digest"),
        ("conversation_enrichment_refs", "input_digest"),
    ],
)
def test_enrichment_unit_rows_are_immutable(fixture, table, column):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE {table} SET {column}={column}")


def test_referenced_enrichment_unit_cannot_be_deleted_without_foreign_keys(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM conversation_enrichment_units")
        db.execute("PRAGMA foreign_keys=ON")


def test_explicit_artifact_read_materializes_complete_enrichments(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    reference = candidate.reference
    assert reference is not None
    fixture.pipeline.publish_candidate(candidate)
    artifact = fixture.stores.projections.read_generation_artifact(ACCOUNT, reference)
    assert artifact.projection.message_enrichments
    assert artifact.projection == candidate.artifact().projection
