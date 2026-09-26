"""Qualify immutable graph units used by incremental account-graph assembly."""

from datetime import timedelta

import pytest

from app.analytics.conversation_graph_units import graph_unit_ids
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, insert_message,
)
from tests.test_shared_graph import fixture


def counts(fixture):
    with fixture.stores.database.read() as db:
        return {
            name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in (
                "conversation_graph_units",
                "conversation_graph_refs",
                "graph_segment_chunks",
                "graph_segments",
            )
        }


def test_cold_build_persists_complete_conversation_graph_units(fixture):
    result = fixture.pipeline.project_account(ACCOUNT)
    state = counts(fixture)
    assert state["conversation_graph_units"] == len(
        result.artifact.projection.conversation_metrics
    )
    assert state["conversation_graph_refs"] == state["conversation_graph_units"]
    assert state["graph_segment_chunks"] == state["graph_segments"]
    metrics = {
        item.conversation_ref: item
        for item in result.artifact.projection.conversation_metrics
    }
    with fixture.stores.database.read() as db:
        generation = db.execute(
            "SELECT generation_id FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        rows = db.execute(
            """SELECT r.*,u.graph_digest,u.node_count,u.edge_count,u.node_ids,u.edge_ids
               FROM conversation_graph_refs r
               JOIN conversation_graph_units u USING(creator_account_id,unit_id)
               WHERE r.generation_id=? ORDER BY r.conversation_ref""",
            (generation,),
        )
        for row in rows:
            from app.analytics.conversation_graph_unit_sql import _header
            from app.analytics.conversation_graph_units import ConversationGraphUnit
            header = _header(row)
            nodes, edges = graph_unit_ids(
                ConversationGraphUnit(header, row["node_ids"], row["edge_ids"])
            )
            source = metrics[header.conversation_ref]
            assert header.participant_ref == source.participant_ref
            assert header.started_at == source.started_at
            assert header.ended_at == source.ended_at
            assert len(nodes) == row["node_count"]
            assert len(edges) == row["edge_count"]


def test_unchanged_generation_reuses_graph_unit_content(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    during = counts(fixture)
    assert during["conversation_graph_units"] == before["conversation_graph_units"]
    assert during["conversation_graph_refs"] == before["conversation_graph_refs"] * 2
    fixture.pipeline.publish_candidate(candidate)


def test_one_changed_conversation_replaces_only_its_unit(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    with fixture.stores.database.read() as db:
        previous = {
            row["conversation_ref"]: row["unit_id"]
            for row in db.execute(
                """SELECT r.conversation_ref,r.unit_id
                   FROM conversation_graph_refs r
                   JOIN projection_generations g USING(generation_id,creator_account_id)
                   WHERE g.status='active'"""
            )
        }
    with fixture.repositories.database.transaction() as db:
        insert_message(db, "chat-1", "new graph-unit message", NOW)
        advance(db)
    fixture.pipeline.project_account(ACCOUNT)
    after = counts(fixture)
    assert after["conversation_graph_units"] == before["conversation_graph_units"]
    with fixture.stores.database.read() as db:
        current = {
            row["conversation_ref"]: row["unit_id"]
            for row in db.execute(
                """SELECT r.conversation_ref,r.unit_id
                   FROM conversation_graph_refs r
                   JOIN projection_generations g USING(generation_id,creator_account_id)
                   WHERE g.status='active'"""
            )
        }
    changed = [ref for ref in current if current[ref] != previous[ref]]
    assert len(changed) == 1


@pytest.mark.parametrize(
    "table,column",
    [
        ("conversation_graph_units", "graph_digest"),
        ("conversation_graph_refs", "input_digest"),
        ("graph_segment_chunks", "canonical_digest"),
    ],
)
def test_incremental_graph_cache_rows_are_immutable(fixture, table, column):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE {table} SET {column}={column}")


@pytest.mark.parametrize(
    "table",
    ["conversation_graph_units", "graph_segment_chunks"],
)
def test_referenced_incremental_graph_cache_cannot_be_deleted_without_foreign_keys(
    fixture, table
):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM " + table)
        db.execute("PRAGMA foreign_keys=ON")


def test_final_generation_cleanup_reclaims_graph_units_and_chunks(fixture):
    fixture.stores.projections.rollback_retention = 0
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections.clear(ACCOUNT)
    state = counts(fixture)
    assert state["conversation_graph_units"] == 0
    assert state["conversation_graph_refs"] == 0
    assert state["graph_segment_chunks"] == 0
    assert state["graph_segments"] == 0


def test_incremental_graph_root_reads_only_changed_predecessor_chunks(
    fixture, monkeypatch
):
    import app.analytics.shared_graph as shared
    from tests.continuous_analytics_fixture import cold_equal

    first = fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        active = db.execute(
            "SELECT generation_id FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        previous = {
            (row["kind"], row["bucket"]): row["segment_id"]
            for row in db.execute(
                """SELECT kind,bucket,segment_id
                   FROM generation_graph_segments WHERE generation_id=?""",
                (active,),
            )
        }

    opened = []
    original = shared.verified_segment_chunk

    def observed(connection, account_id, proof, kind, bucket):
        opened.append((kind, bucket))
        return original(connection, account_id, proof, kind, bucket)

    monkeypatch.setattr(shared, "verified_segment_chunk", observed)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, "chat-1", "segment-root-changed-message", NOW)
        advance(db)

    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        current = {
            (row["kind"], row["bucket"]): row["segment_id"]
            for row in db.execute(
                """SELECT kind,bucket,segment_id
                   FROM generation_graph_segments WHERE generation_id=?""",
                (candidate.staged_generation_id,),
            )
        }
    expected = {
        key for key, segment_id in current.items()
        if key in previous and previous[key] != segment_id
    }
    assert set(opened) == expected
    assert len(opened) == len(expected)

    result = fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, result.artifact)
    assert first.artifact.projection.graph_digest != result.artifact.projection.graph_digest
