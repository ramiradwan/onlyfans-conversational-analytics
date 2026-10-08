"""Compare packaged persisted analytics with a read-only canonical replay."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.conversation_reuse import ACTIVE_CONVERSATIONS, ConversationBuild
from app.analytics.errors import CanonicalRevisionChanged
from app.analytics.generation_reference import GenerationReference, check_reference
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_contracts import utc_instant
from app.analytics.rebuild import ReadOnlyCanonicalDatabase, _file_identity, _safe_absolute_path
from app.analytics.sqlite_projection_store import (
    SQLiteAnalyticsProjectionStore, recompute_generation, verify_generation_values,
)
from app.analytics.validation_receipt import content_stamp, generation_binding
from app.persistence.database import ProjectionsSQLite
from app.persistence.history import HistoryRepository
from app.persistence.migrations import load_migration_catalog, resolve_migration_catalog
from app.persistence.projection_activation import SQLiteProjectionActivationRepository
from app.security.local_data_key import MASTER_KEY_FILENAME
from tools import analytics_qualification as q


_DIGESTS = ("projection_digest", "graph_digest", "canonical_content_digest")
_ACCOUNT = "synthetic-continuous-owner"


def _paths(inputs):
    if inputs.get("synthetic_account_id") != _ACCOUNT:
        raise ValueError("dedicated_synthetic_account_required")
    directory = _safe_absolute_path(inputs["data_directory"])
    runtime = _safe_absolute_path(inputs["runtime_directory"])
    if not directory.is_dir() or directory == runtime or runtime in directory.parents:
        raise ValueError("oracle_data_directory_invalid")
    configuration = _safe_absolute_path(directory / "runtime.env")
    if not configuration.is_file():
        raise ValueError("oracle_configuration_missing")
    values = {key.lower(): value for key, value in
              dotenv_values(configuration, interpolate=False).items()}
    paths = []
    for field in ("canonical_database_path", "analytics_projection_database_path"):
        value = values.get(field)
        if not value or not Path(value).is_absolute():
            raise ValueError("oracle_database_configuration_missing")
        path = _safe_absolute_path(value)
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError("oracle_database_path_invalid")
        key = _safe_absolute_path(path.parent / MASTER_KEY_FILENAME)
        if not key.is_file():
            raise ValueError("oracle_existing_key_required")
        paths.append(path)
    if paths[0] == paths[1]:
        raise ValueError("oracle_database_paths_shared")
    return paths


@contextmanager
def _projection_snapshot(path):
    identity = _file_identity(path)
    database = ProjectionsSQLite(path, key_scope="analytics-projection")
    connection = database.open_detached(path, read_only=True, query_only=True)
    try:
        if identity != _file_identity(path):
            raise ValueError("oracle_projection_identity_changed")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        yield connection
        if identity != _file_identity(_safe_absolute_path(path)):
            raise ValueError("oracle_projection_identity_changed")
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


@contextmanager
def _read(connection):
    yield connection


def _validate_projection_schema(connection):
    catalog = load_migration_catalog(Path(__file__).resolve().parents[1]
                                     / "app" / "analytics" / "sql")
    rows = connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    if ([tuple(row) for row in rows] != [(m.version, m.name, m.checksum) for m in catalog]
            or connection.execute("PRAGMA user_version").fetchone()[0] != len(catalog)
            or content_stamp(connection) is None):
        raise ValueError("oracle_projection_schema_invalid")
    if connection.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
        raise ValueError("oracle_projection_integrity_invalid")


def _integrity(connection):
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("oracle_foreign_key_failure")
    pairs = (
        ("graph_node_content", "graph_membership_nodes", "content_id"),
        ("graph_edge_content", "graph_membership_edges", "content_id"),
        ("graph_membership_pages", "graph_segment_membership_pages", "page_id"),
        ("conversation_page_content", "conversation_page_refs", "content_id"),
        ("enrichment_content", "enrichment_refs", "content_id"),
        ("conversation_graph_units", "conversation_graph_refs", "unit_id"),
        ("conversation_enrichment_units", "conversation_enrichment_refs", "unit_id"),
    )
    for content, references, key in pairs:
        if connection.execute(
            f"SELECT 1 FROM {content} c WHERE NOT EXISTS (SELECT 1 FROM {references} r "
            f"WHERE r.creator_account_id=c.creator_account_id AND r.{key}=c.{key}) LIMIT 1"
        ).fetchone() is not None:
            raise ValueError("oracle_orphan_content")


def _stale_reference_rejected(connection, canonical, previous, current):
    if previous is None:
        return None
    if previous == current:
        raise ValueError("oracle_previous_generation_is_current")
    row = connection.execute(
        "SELECT g.*,q.projection_generation,q.first_source FROM projection_generations g "
        "JOIN projection_query_metadata q USING(generation_id,creator_account_id) "
        "WHERE g.generation_id=? AND g.creator_account_id=?", (previous, account_ref(_ACCOUNT))
    ).fetchone()
    if row is None:
        return True
    reference = GenerationReference(
        previous, row["creator_account_id"], row["canonical_revision"],
        row["projection_generation"], row["canonical_content_digest"], row["pipeline_revision"],
        row["pipeline_config_digest"], row["pipeline_identity_digest"], row["projection_digest"],
        row["graph_digest"], row["publication_epoch"],
        utc_instant(row["first_source"]) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
        if row["first_source"] else None,
    )
    store = SimpleNamespace(
        database=SimpleNamespace(read=lambda: _read(connection)),
        activation=SQLiteProjectionActivationRepository(canonical),
        _intent_matches=SQLiteAnalyticsProjectionStore._intent_matches,
    )
    try:
        check_reference(store, _ACCOUNT, reference)
    except CanonicalRevisionChanged:
        return True
    raise ValueError("oracle_stale_reference_readable")


def _cold_projection(source, generation, now):
    pipeline = AnalyticsPipeline(source, clock=lambda: now,
                                 reuse_enrichment=False, reuse_conversations=False)
    state = ConversationBuild()
    state.compact = True
    token = ACTIVE_CONVERSATIONS.set(state)
    try:
        snapshot = source.analytics_snapshot(_ACCOUNT)
        return pipeline._build(_ACCOUNT, snapshot, projection_generation=generation).projection
    finally:
        ACTIVE_CONVERSATIONS.reset(token)


def message_digest(rows):
    """Hash sorted canonical message fields without retaining their content."""
    digest = hashlib.sha256(b"analytics-package-input.v1\0")
    count, previous = 0, None
    for row in rows:
        if (len(row) != 6 or any(not isinstance(row[index], str) for index in (0, 1, 3, 4, 5))
                or (row[2] is not None and not isinstance(row[2], str))
                or (previous is not None and row[0] <= previous)):
            raise ValueError("oracle_fixture_record_invalid")
        value = list(row)
        value[4] = utc_instant(value[4]).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        digest.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        previous = row[0]
        count += 1
    return count, digest.hexdigest()


def _fixture_content(connection, expected_messages):
    if expected_messages is None:
        return {"fixture_content_equal": None, "expected_message_count": None,
                "expected_messages_sha256": None, "actual_messages_sha256": None}
    expected_count, expected_digest = message_digest(expected_messages)
    rows = connection.execute(
        "SELECT message_id,chat_id,sender_platform_user_id,text,sent_at,direction "
        "FROM account_messages WHERE creator_account_id=? AND is_deleted=0 ORDER BY message_id",
        (_ACCOUNT,),
    )
    try:
        actual_count, actual_digest = message_digest(rows)
    finally:
        rows.close()
    if (expected_count, expected_digest) != (actual_count, actual_digest):
        raise ValueError("oracle_fixture_content_mismatch")
    return {"fixture_content_equal": True, "expected_message_count": expected_count,
            "expected_messages_sha256": expected_digest, "actual_messages_sha256": actual_digest}


def verify_package(inputs: dict, generation_id: str, source_revision: int,
                   output_path: Path, previous_generation_id: str | None = None, *,
                   expected_messages=None) -> dict:
    """Read existing encrypted stores and write only the independent evidence record."""
    if not isinstance(generation_id, str) or not generation_id or type(source_revision) is not int:
        raise ValueError("oracle_generation_binding_invalid")
    canonical_path, projection_path = _paths(inputs)
    output = _safe_absolute_path(output_path)
    if output.is_relative_to(_safe_absolute_path(inputs["data_directory"])):
        raise ValueError("oracle_output_inside_data_directory")
    if output.is_relative_to(_safe_absolute_path(inputs["runtime_directory"])):
        raise ValueError("oracle_output_inside_runtime_directory")
    now = datetime.now(timezone.utc)
    with ReadOnlyCanonicalDatabase(canonical_path) as canonical, _projection_snapshot(projection_path) as db:
        versions = (canonical.connection.execute("PRAGMA data_version").fetchone()[0],
                    db.execute("PRAGMA data_version").fetchone()[0])
        canonical.validate_schema()
        if (canonical.connection.execute("PRAGMA user_version").fetchone()[0]
                != len(resolve_migration_catalog(canonical.connection))):
            raise ValueError("oracle_canonical_schema_incomplete")
        if canonical.connection.execute(
            "SELECT 1 FROM account_heads WHERE creator_account_id<>? LIMIT 1", (_ACCOUNT,)
        ).fetchone() is not None:
            raise ValueError("oracle_dedicated_account_required")
        fixture_content = _fixture_content(canonical.connection, expected_messages)
        _validate_projection_schema(db)
        source = HistoryAnalyticsSource(HistoryRepository.__new__(HistoryRepository),
                                        connection=canonical.connection)
        generation = db.execute(
            "SELECT * FROM projection_generations WHERE generation_id=? "
            "AND creator_account_id=? AND status='active' AND activated_at IS NOT NULL",
            (generation_id, account_ref(_ACCOUNT)),
        ).fetchone()
        if generation is None or generation["canonical_revision"] != source_revision:
            raise ValueError("oracle_active_generation_mismatch")
        witness = SQLiteProjectionActivationRepository(canonical).get(generation_id)
        if (not SQLiteAnalyticsProjectionStore._intent_matches(
                generation, witness, require_completed=True) or witness.creator_account_id != _ACCOUNT):
            raise ValueError("oracle_canonical_witness_mismatch")
        actual_values = recompute_generation(db, generation_id, materialize_projection=False)
        verify_generation_values(generation, actual_values)
        actual_projection = actual_values["projection"]
        expected_projection = _cold_projection(source, actual_projection.projection_generation, now)
        expected = {field: getattr(expected_projection, field) for field in _DIGESTS}
        actual = {field: (actual_values[field] if field in actual_values
                          else getattr(actual_projection, field)) for field in _DIGESTS}
        if expected != actual or expected_projection.source_revision != source_revision:
            raise ValueError("oracle_independent_rebuild_mismatch")
        stale = _stale_reference_rejected(db, canonical, previous_generation_id, generation_id)
        _integrity(db)
        binding = generation_binding(generation)
        canonical.verify_identity()
        canonical.connection.rollback()
        db.rollback()
        if (canonical.connection.total_changes != 0 or db.total_changes != 0
                or versions != (canonical.connection.execute("PRAGMA data_version").fetchone()[0],
                                db.execute("PRAGMA data_version").fetchone()[0])):
            raise ValueError("oracle_source_changed_during_verification")
    result = {
        "schema": "analytics-package-oracle.v1", "checked_at": now.isoformat(),
        "generation_id": generation_id, "source_revision": source_revision,
        "previous_generation_id": previous_generation_id,
        "expected": expected, "actual": actual,
        "canonical_unchanged": True, "independent_rebuild_equal": True,
        "persisted_content_revalidated": True, "no_unpermitted_orphans": True,
        "stale_reference_rejected": stale,
        **fixture_content,
        "persisted_verifier_binding": {"generation_id": generation_id,
            "canonical_revision": source_revision, "generation_binding": binding, **actual},
    }
    q.write_once(output, result)
    return result
