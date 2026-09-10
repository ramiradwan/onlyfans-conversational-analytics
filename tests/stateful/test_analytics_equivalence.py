"""Task 6B: real incremental analytics converges with a clean rebuild.

Each command goes through ``HistoryRepository.commit_delta``.  After every
delivery the same live ``AnalyticsPipeline`` takes its normal incremental
publication path.  Clean comparisons always use fresh in-memory derived
stores over the final committed canonical witness.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from hypothesis import HealthCheck, Phase, given, settings, strategies as st

from app.analytics.analyzers import RuleBasedSentimentAnalyzer
from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.enrichment import EnrichmentStage
from app.analytics.errors import CanonicalRevisionChanged
from app.analytics.pipeline import AnalyticsPipeline
from app.models.analytics import RebuildArtifact
from app.persistence.database import CanonicalSQLite
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.history import HistoryRepository, IngestResult, StreamKey
from app.persistence import sqlite_api
from app.protocol.common import (
    ChatDeleteChange,
    ChatUpsertChange,
    MessageDeleteChange,
    MessageUpsertChange,
    RawChat,
    RawMessage,
)
from app.protocol.payloads import (
    IngestDeltaPayload,
    IngestSnapshotBeginPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)
from tests.hardening.falsifiers.analytics_falsifiers import (
    BrokenDeletionClosureAdapter,
    BrokenDerivedDeletionAdapter,
    ForgedTopicEntityIdentityAdapter,
    BrokenGraphAdapter,
    BrokenIdentityAdapter,
    BrokenMetricAdapter,
    BrokenProvenanceAdapter,
)
from tests.state_models.analytics_oracle import (
    AnalyticsOracleMismatch,
    ReproducibilityContext,
    assert_active_publication_witness,
    assert_deleted_material_absent,
    assert_incremental_rebuild_convergence,
    context_for,
    expected_active_refs,
    expected_semantic_from_canonical,
    normalize_convergence_artifact,
)


EVALUATION_CLOCK = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
CREATOR_ID = "task6b-synthetic-creator"
CONNECTION_ID = UUID("71000000-0000-4000-8000-000000000001")
INSTALLATION_ID = UUID("72000000-0000-4000-8000-000000000001")
STREAM_ID = UUID("73000000-0000-4000-8000-000000000001")
STREAM = StreamKey(CREATOR_ID, INSTALLATION_ID, STREAM_ID)


def history_source_for(repositories: CanonicalRepositories) -> HistoryAnalyticsSource:
    """Construct the canonical analytics adapter at this test composition seam."""

    return HistoryAnalyticsSource(repositories.history)


TEXTS = (
    "hello, thank you",
    "the price is $25 #support",
    "can we schedule tomorrow?",
    "https://example.invalid/media @creator",
    "thanks, that works",
)


for profile, examples in (
    # Every example contains every operation family.  The fast counts are the
    # measured local CI calibration; the research-gate 30/20 breadth remains
    # available as explicit stress profiles for fuller offline calibration.
    ("task6b_convergence_fast", 6),
    ("task6b_convergence_stress", 30),
    ("task6b_convergence_dev", 2),
    ("task6_deletion_fast", 4),
    ("task6_deletion_stress", 20),
    ("task6_deletion_dev", 2),
):
    settings.register_profile(
        profile,
        max_examples=examples,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "task6b_convergence_dev"))


def _runtime_metadata() -> dict[str, str]:
    """Measure, rather than transcribe, the runtime behind one generated run."""

    connection = sqlite_api.connect(":memory:")
    try:
        cipher_version = sqlite_api.require_cipher(connection)
    finally:
        connection.close()
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "hypothesis_version": __import__("hypothesis").__version__,
        "sqlite_runtime_version": sqlite_api.sqlite_version,
        "sqlcipher_version": cipher_version,
        "platform": platform.platform(),
    }


def _read_task6b_metrics() -> tuple[Path, dict[str, Any]] | None:
    raw_path = os.environ.get("TASK6B_METRICS_PATH")
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.exists():
        return path, json.loads(path.read_text(encoding="utf-8"))
    return path, {
        "schema_version": "task6b-runtime-metrics.v1",
        "runtime": _runtime_metadata(),
        "runs": {},
        "deliberate_falsifier": None,
    }


def _write_task6b_metrics(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _record_generated_history(
    suite: str,
    state: dict[str, Any],
    *,
    wall_clock_seconds: float,
) -> None:
    """Persist actual generated work only when an opted-in run passes."""

    target = _read_task6b_metrics()
    if target is None:
        return
    path, document = target
    trace = state["trace"]
    deliveries = [
        entry
        for entry in trace
        if entry["operation"]
        in {
            "canonical_delta",
            "duplicate_replay",
            "duplicate_delete_replay",
            "tombstoned_reappearance",
        }
    ]
    deletion_deliveries = [
        entry
        for entry in deliveries
        if str(entry["frame"]["change"]["type"]).endswith(".delete")
    ]
    accepted_deletions = [
        entry for entry in deletion_deliveries if entry["outcome"] == "accepted"
    ]
    run = document["runs"].setdefault(
        suite,
        {
            "hypothesis_profile": os.environ.get("HYPOTHESIS_PROFILE"),
            "actual_history_count": 0,
            "actual_canonical_mutation_deliveries": 0,
            "actual_clean_rebuild_count": 0,
            "actual_deletion_delivery_count": 0,
            "actual_accepted_deletion_count": 0,
            "actual_duplicate_delivery_count": 0,
            "actual_repository_restart_count": 0,
            "actual_alternative_order_history_count": 0,
            "history_wall_clock_seconds": [],
        },
    )
    run["actual_history_count"] += 1
    run["actual_canonical_mutation_deliveries"] += len(deliveries)
    run["actual_clean_rebuild_count"] += state["clean_rebuild_count"]
    run["actual_deletion_delivery_count"] += len(deletion_deliveries)
    run["actual_accepted_deletion_count"] += len(accepted_deletions)
    run["actual_duplicate_delivery_count"] += sum(
        entry["outcome"] == "duplicate" for entry in deliveries
    )
    run["actual_repository_restart_count"] += state["repository_restart_count"]
    run["actual_alternative_order_history_count"] += state[
        "alternative_order_history_count"
    ]
    run["history_wall_clock_seconds"].append(round(wall_clock_seconds, 6))
    _write_task6b_metrics(path, document)


def _record_deliberate_falsifier(
    *,
    generated_calls: int,
    wall_clock_seconds: float,
) -> None:
    """Record the actual expected-failure invocation without phase invention."""

    target = _read_task6b_metrics()
    if target is None:
        return
    path, document = target
    unavailable = (
        "Hypothesis does not expose a first-failure or post-first-failure "
        "shrink boundary to this expected-exception test."
    )
    document["deliberate_falsifier"] = {
        "status": "expected_failure_with_generate_and_shrink_configured",
        "actual_generated_call_count": generated_calls,
        "wall_clock_seconds": round(wall_clock_seconds, 6),
        "generation_to_first_failure_seconds": None,
        "generation_to_first_failure_reason": unavailable,
        "shrink_phase_seconds": None,
        "shrink_phase_reason": unavailable,
    }
    _write_task6b_metrics(path, document)


def _uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ofca-task6b:{label}")


def _at(days: int, seconds: int = 0) -> datetime:
    return EVALUATION_CLOCK + timedelta(days=days, seconds=seconds)


def _chat_change(
    conversation_id: str,
    participant_id: str,
    *,
    display_name: str,
    update_offset: int,
) -> ChatUpsertChange:
    return ChatUpsertChange(
        type="chat.upsert",
        chat=RawChat(
            record_kind="full",
            chat_id=conversation_id,
            platform_user_id=participant_id,
            display_name=display_name,
            updated_at=_at(update_offset),
        ),
    )


def _message_change(
    message_id: str,
    conversation_id: str,
    *,
    sent_days: int,
    sent_seconds: int,
    direction: str,
    text: str,
) -> MessageUpsertChange:
    return MessageUpsertChange(
        type="message.upsert",
        message=RawMessage(
            message_id=message_id,
            chat_id=conversation_id,
            sender_platform_user_id=(
                "creator-platform" if direction == "outbound" else "fan-platform"
            ),
            text=text,
            sent_at=_at(sent_days, sent_seconds),
            direction=direction,
        ),
    )


def _delete_message(message_id: str, conversation_id: str) -> MessageDeleteChange:
    return MessageDeleteChange(
        type="message.delete", message_id=message_id, chat_id=conversation_id
    )


def _delete_chat(conversation_id: str) -> ChatDeleteChange:
    return ChatDeleteChange(type="chat.delete", chat_id=conversation_id)


def _payload(sequence: int, label: str, change: Any) -> IngestDeltaPayload:
    return IngestDeltaPayload(
        connection_id=CONNECTION_ID,
        fencing_token="task6b-fencing-token",
        creator_account_id=CREATOR_ID,
        agent_installation_id=INSTALLATION_ID,
        event_id=_uuid(label),
        agent_stream_id=STREAM_ID,
        source_seq=sequence,
        acquisition_origin="signer",
        change=change,
    )


def _initialize_canonical_stream(
    repositories: CanonicalRepositories, trace: list[dict[str, Any]]
) -> None:
    """Establish the real snapshot-required stream before delivering deltas."""

    snapshot_id = _uuid("task6b-empty-canonical-baseline")
    identity = {
        "connection_id": CONNECTION_ID,
        "fencing_token": "task6b-fencing-token",
        "creator_account_id": CREATOR_ID,
        "agent_installation_id": INSTALLATION_ID,
        "agent_stream_id": STREAM_ID,
        "snapshot_id": snapshot_id,
    }
    begin = IngestSnapshotBeginPayload(
        **identity,
        frame_kind="begin",
        through_seq=0,
        chunk_count=0,
        record_counts=SnapshotRecordCounts(chats=0, messages=0, coverage_evidence=0),
        max_frame_bytes=524288,
    )
    committed = IngestSnapshotCommitPayload(
        **identity, frame_kind="commit", chunk_count=0
    )
    begin_result = repositories.history.begin_snapshot(STREAM, begin)
    commit_result = repositories.history.commit_snapshot(STREAM, committed)
    assert begin_result.status == "accepted"
    assert commit_result.status == "accepted"
    trace.extend(
        [
            {
                "operation": "initial_snapshot_begin",
                "outcome": begin_result.status,
                "frame": begin.model_dump(mode="json"),
            },
            {
                "operation": "initial_snapshot_commit",
                "outcome": commit_result.status,
                "frame": committed.model_dump(mode="json"),
            },
        ]
    )


def _analyzer_context(pipeline: AnalyticsPipeline) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (item.analyzer_name, item.revision, item.config_digest)
            for item in pipeline.enrichment.provenance([])
        )
    )


def _context(pipeline: AnalyticsPipeline, *, seed: int) -> ReproducibilityContext:
    account = pipeline.source.account_read_model(CREATOR_ID)
    return context_for(
        CREATOR_ID,
        account,
        pipeline_revision=pipeline.pipeline_revision,
        pipeline_config_digest=pipeline.pipeline_config_digest,
        analyzer_provenance=_analyzer_context(pipeline),
        evaluation_clock=EVALUATION_CLOCK,
        deterministic_seed=seed,
    )


def _publication_witness(
    pipeline: AnalyticsPipeline, context: ReproducibilityContext
) -> dict[str, Any]:
    account = pipeline.source.account_read_model(CREATOR_ID)
    active = pipeline.active_projection(CREATOR_ID, account)
    return {
        "canonical_revision": account.view_revision,
        "canonical_content_digest": context.canonical_content_digest,
        "active_source_revision": None if active is None else active.source_revision,
        "active_content_digest": (
            None if active is None else active.canonical_content_digest
        ),
        "active_graph_revision": (
            None
            if active is None
            else pipeline.graph.partition_revision(active.account_ref)
        ),
    }


def _read_active_publication(
    pipeline: AnalyticsPipeline, account: Any
) -> tuple[Any | None, list[Any], list[Any], int | None]:
    """Read complete active projection and graph material from live stores."""

    active = pipeline.active_projection(CREATOR_ID, account)
    if active is None:
        return None, [], [], None
    return (
        active,
        pipeline.graph.nodes(active.account_ref),
        pipeline.graph.edges(active.account_ref),
        pipeline.graph.partition_revision(active.account_ref),
    )


def _assert_current_publication(
    pipeline: AnalyticsPipeline,
    context: ReproducibilityContext,
    expected_semantics: dict[str, Any],
) -> None:
    account = pipeline.source.account_read_model(CREATOR_ID)
    active, nodes, edges, graph_revision = _read_active_publication(pipeline, account)
    assert_active_publication_witness(
        active_projection=active,
        active_nodes=nodes,
        active_edges=edges,
        active_graph_revision=graph_revision,
        context=context,
        expected_semantics=expected_semantics,
    )


def _reproduction(
    *,
    trace: list[dict[str, Any]],
    context: ReproducibilityContext,
    expected: Any,
    actual: Any,
    incremental: AnalyticsPipeline,
) -> str:
    """Return complete JSON-safe failure material instead of an opaque example."""

    payload = {
        "creator_account_id": CREATOR_ID,
        "canonical_command_trace": trace,
        "reproducibility_context": context.json_safe(),
        "expected_normalized_semantic_provenance_shape": normalize_convergence_artifact(
            expected
        ),
        "actual_normalized_semantic_provenance_shape": normalize_convergence_artifact(
            actual
        ),
        "final_publication_witness": _publication_witness(incremental, context),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _generated_failure_reproduction(
    history: dict[str, Any], state: dict[str, Any], error: BaseException
) -> str:
    """Preserve a complete replay record for failures outside a clean compare."""

    trace = state.get("trace", [])
    pipeline = state.get("pipeline")
    repositories = state.get("repositories")
    actual = state.get("artifact")
    context: ReproducibilityContext | None = None
    rebuilt: Any | None = None
    if pipeline is not None:
        try:
            context = _context(pipeline, seed=int(history["seed"]))
            if repositories is not None:
                rebuilt = AnalyticsPipeline(
                    history_source_for(repositories), clock=lambda: EVALUATION_CLOCK
                ).rebuild_account(CREATOR_ID).artifact
        except Exception as capture_error:
            capture_error_text = repr(capture_error)
        else:
            capture_error_text = None
    else:
        capture_error_text = "live incremental pipeline was not initialized"
    payload = {
        "creator_account_id": CREATOR_ID,
        "history_parameters": history,
        "canonical_command_trace": trace,
        "reproducibility_context": None if context is None else context.json_safe(),
        "expected_normalized_semantic_provenance_shape": (
            None if rebuilt is None else normalize_convergence_artifact(rebuilt)
        ),
        "actual_normalized_semantic_provenance_shape": (
            None if actual is None else normalize_convergence_artifact(actual)
        ),
        "final_publication_witness": (
            None
            if pipeline is None or context is None
            else _publication_witness(pipeline, context)
        ),
        "failure": repr(error),
        "reproduction_capture_error": capture_error_text,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _run_generated_history(
    history: dict[str, Any], runner: Any
) -> None:
    """Guarantee JSON-safe command/context/output material for any property failure."""

    state: dict[str, Any] = {}
    started = perf_counter()
    try:
        runner(history, state=state)
        _record_generated_history(
            "general" if runner is _run_general else "deletion",
            state,
            wall_clock_seconds=perf_counter() - started,
        )
    except AssertionError as error:
        # _clean_compare already constructs the required complete record.
        if str(error).startswith("{"):
            raise
        raise AssertionError(_generated_failure_reproduction(history, state, error)) from error
    except Exception as error:
        raise AssertionError(_generated_failure_reproduction(history, state, error)) from error


def _clean_compare(
    repositories: CanonicalRepositories,
    incremental: AnalyticsPipeline,
    incremental_artifact: Any,
    *,
    seed: int,
    trace: list[dict[str, Any]],
) -> tuple[Any, ReproducibilityContext]:
    """Build fresh derived stores and compare them with the active live path."""

    context = _context(incremental, seed=seed)
    history_source = history_source_for(repositories)
    account = history_source.account_read_model(CREATOR_ID)
    clean = AnalyticsPipeline(history_source, clock=lambda: EVALUATION_CLOCK)
    rebuilt = clean.rebuild_account(CREATOR_ID).artifact
    expected_semantics = expected_semantic_from_canonical(
        CREATOR_ID, account, context
    )
    try:
        assert_incremental_rebuild_convergence(
            incremental_artifact,
            rebuilt,
            context,
            expected_refs=expected_active_refs(CREATOR_ID, account, context),
            expected_semantics=expected_semantics,
        )
        _assert_current_publication(
            incremental, context, expected_semantics
        )
        _assert_current_publication(clean, context, expected_semantics)
    except AnalyticsOracleMismatch as error:
        raise AssertionError(
            _reproduction(
                trace=trace,
                context=context,
                expected=rebuilt,
                actual=incremental_artifact,
                incremental=incremental,
            )
        ) from error
    return rebuilt, context


def _apply(
    repositories: CanonicalRepositories,
    pipeline: AnalyticsPipeline,
    payload: IngestDeltaPayload,
    *,
    operation: str,
    trace: list[dict[str, Any]],
) -> tuple[IngestResult, Any]:
    """Commit through canonical authority, then advance the live pipeline once."""

    outcome = repositories.history.commit_delta(STREAM, payload)
    assert outcome.status in {"accepted", "duplicate"}
    run = pipeline.project_account(CREATOR_ID)
    account = pipeline.source.account_read_model(CREATOR_ID)
    assert run.artifact.projection.source_revision == account.view_revision
    trace.append(
        {
            "operation": operation,
            "outcome": outcome.status,
            "canonical_revision": outcome.canonical_revision,
            "pipeline_changed": run.changed,
            "frame": payload.model_dump(mode="json"),
        }
    )
    return outcome, run.artifact


def _restart_incremental_pipeline(
    repositories: CanonicalRepositories,
    *,
    trace: list[dict[str, Any]],
) -> tuple[AnalyticsPipeline, Any]:
    """Reconstruct canonical access over the same temporary SQLCipher database."""

    # A restart claim must reconstruct the authority boundary, rather than
    # merely invoke another method on its already-live HistoryRepository.
    restarted_database = CanonicalSQLite(
        repositories.database.path,
        busy_timeout_ms=repositories.database.busy_timeout_ms,
        key_scope=repositories.database.key_scope,
    )
    assert restarted_database is not repositories.database
    assert restarted_database.path == repositories.database.path
    assert restarted_database.busy_timeout_ms == repositories.database.busy_timeout_ms
    assert restarted_database.key_scope == repositories.database.key_scope
    assert restarted_database._encryption_key == repositories.database._encryption_key
    restarted_history = HistoryRepository(restarted_database)
    restarted_source = HistoryAnalyticsSource(restarted_history)
    restarted = AnalyticsPipeline(restarted_source, clock=lambda: EVALUATION_CLOCK)
    run = restarted.project_account(CREATOR_ID)
    account = restarted_source.account_read_model(CREATOR_ID)
    assert run.artifact.projection.source_revision == account.view_revision
    # CanonicalSQLite owns no persistent handle; HistoryAnalyticsSource uses a
    # fresh ``database.read()`` connection for each operation and closes it.
    assert CanonicalSQLite.open_connection_count(restarted_database.path) == 0
    trace.append(
        {
            "operation": "canonical_repository_restart",
            "outcome": "reconstructed",
            "canonical_revision": account.view_revision,
            "pipeline_changed": run.changed,
            "canonical_storage": "same_temporary_file_backed_sqlcipher_database",
            "canonical_database_wrapper": "distinct_same_path_key_scope_and_timeout",
            "per_operation_connections_closed": True,
        }
    )
    return restarted, run.artifact


@st.composite
def general_histories(draw: st.DrawFn) -> dict[str, Any]:
    extras = []
    for index in range(4):
        extras.append(
            {
                "conversation_id": draw(st.sampled_from(("general-a", "general-b", "general-c"))),
                "sent_days": draw(st.sampled_from((-91, -90, -89, -2, -1, 0))),
                "sent_seconds": draw(st.integers(min_value=0, max_value=3_600)),
                "direction": draw(st.sampled_from(("inbound", "outbound"))),
                "text": draw(st.sampled_from(TEXTS)),
                "index": index,
            }
        )
    return {
        "extras": extras,
        # Both choices differ from the declaration order.  This makes every
        # generated history exercise a valid alternative delivery ordering.
        "chat_delivery_order": draw(
            st.sampled_from(
                (("general-b", "general-a", "general-c"), ("general-c", "general-a", "general-b"))
            )
        ),
        "updated_name": draw(st.sampled_from(("Shared newer", "Shared final"))),
        "seed": draw(st.integers(min_value=0, max_value=2**32 - 1)),
    }


def _run_general(
    history: dict[str, Any], *, state: dict[str, Any] | None = None
) -> tuple[Any, Any, ReproducibilityContext, list[dict[str, Any]]]:
    repositories = create_canonical_repositories("memory")
    incremental = AnalyticsPipeline(history_source_for(repositories), clock=lambda: EVALUATION_CLOCK)
    trace: list[dict[str, Any]] = []
    _initialize_canonical_stream(repositories, trace)
    if state is not None:
        state.update(
            {
                "repositories": repositories,
                "pipeline": incremental,
                "trace": trace,
                "clean_rebuild_count": 0,
                "repository_restart_count": 0,
                "alternative_order_history_count": 1,
            }
        )
    sequence = 1
    artifacts: list[Any] = []

    chat_changes = {
        "general-a": ("chat-a", _chat_change("general-a", "fan-shared", display_name="Shared A", update_offset=-5)),
        "general-b": ("chat-b", _chat_change("general-b", "fan-shared", display_name="Shared B", update_offset=-4)),
        "general-c": ("chat-c", _chat_change("general-c", "fan-other", display_name="Other", update_offset=-3)),
    }
    changes = [
        chat_changes[conversation_id]
        for conversation_id in history["chat_delivery_order"]
    ] + [
        ("old", _message_change("general-old", "general-a", sent_days=-90, sent_seconds=0, direction="inbound", text=TEXTS[0])),
        ("active-a", _message_change("general-active-a", "general-a", sent_days=-1, sent_seconds=30, direction="inbound", text=TEXTS[1])),
        ("active-b", _message_change("general-active-b", "general-b", sent_days=-2, sent_seconds=20, direction="outbound", text=TEXTS[2])),
        ("active-c", _message_change("general-active-c", "general-c", sent_days=0, sent_seconds=10, direction="inbound", text=TEXTS[3])),
    ]
    for extra in history["extras"]:
        changes.append(
            (
                f"extra-{extra['index']}",
                _message_change(
                    f"general-extra-{extra['index']}",
                    extra["conversation_id"],
                    sent_days=extra["sent_days"],
                    sent_seconds=extra["sent_seconds"],
                    direction=extra["direction"],
                    text=extra["text"],
                ),
            )
        )
    changes.extend(
        [
            ("updated-chat", _chat_change("general-a", "fan-shared", display_name=history["updated_name"], update_offset=1)),
            ("delete-extra", _delete_message("general-extra-1", history["extras"][1]["conversation_id"])),
        ]
    )
    trace.append(
        {
            "operation": "alternative_delivery_order",
            "outcome": "selected",
            "conversation_order": list(history["chat_delivery_order"]),
        }
    )
    for label, change in changes:
        _, artifact = _apply(
            repositories,
            incremental,
            _payload(sequence, f"general-{label}", change),
            operation="canonical_delta",
            trace=trace,
        )
        artifacts.append(artifact)
        if state is not None:
            state["artifact"] = artifact
        sequence += 1
        if len(artifacts) == 7:
            _clean_compare(
                repositories,
                incremental,
                artifact,
                seed=history["seed"],
                trace=trace,
            )
            if state is not None:
                state["clean_rebuild_count"] += 1
            incremental, artifact = _restart_incremental_pipeline(
                repositories, trace=trace
            )
            if state is not None:
                state["pipeline"] = incremental
                state["artifact"] = artifact
                state["repository_restart_count"] += 1

    replay = _payload(5, "general-active-a", changes[4][1])
    outcome, incremental_artifact = _apply(
        repositories,
        incremental,
        replay,
        operation="duplicate_replay",
        trace=trace,
    )
    assert outcome.status == "duplicate"
    assert trace[-1]["pipeline_changed"] is False
    if state is not None:
        state["artifact"] = incremental_artifact
    rebuilt, context = _clean_compare(
        repositories,
        incremental,
        incremental_artifact,
        seed=history["seed"],
        trace=trace,
    )
    if state is not None:
        state["clean_rebuild_count"] += 1
    return incremental_artifact, rebuilt, context, trace


@st.composite
def deletion_histories(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "chat_delivery_order": draw(
            st.sampled_from(
                (
                    ("delete-message", "delete-keep", "delete-conversation"),
                    ("delete-conversation", "delete-keep", "delete-message"),
                )
            )
        ),
        "keep_direction": draw(st.sampled_from(("inbound", "outbound"))),
        "delete_direction": draw(st.sampled_from(("inbound", "outbound"))),
        "first_seconds": draw(st.integers(min_value=0, max_value=120)),
        "second_seconds": draw(st.integers(min_value=121, max_value=600)),
        "seed": draw(st.integers(min_value=0, max_value=2**32 - 1)),
    }


def _run_deletion(
    history: dict[str, Any], *, state: dict[str, Any] | None = None
) -> tuple[Any, Any, ReproducibilityContext, list[dict[str, Any]], Any, CanonicalRepositories]:
    repositories = create_canonical_repositories("memory")
    incremental = AnalyticsPipeline(history_source_for(repositories), clock=lambda: EVALUATION_CLOCK)
    trace: list[dict[str, Any]] = []
    _initialize_canonical_stream(repositories, trace)
    if state is not None:
        state.update(
            {
                "repositories": repositories,
                "pipeline": incremental,
                "trace": trace,
                "clean_rebuild_count": 0,
                "repository_restart_count": 0,
                "alternative_order_history_count": 1,
            }
        )
    sequence = 1
    pre_chat_delete: Any | None = None
    chat_changes = {
        "delete-keep": ("chat-keep", _chat_change("delete-keep", "fan-keep", display_name="Keep", update_offset=-5)),
        "delete-message": ("chat-message", _chat_change("delete-message", "fan-message", display_name="Message only", update_offset=-4)),
        "delete-conversation": ("chat-conversation", _chat_change("delete-conversation", "fan-last", display_name="Last reference", update_offset=-3)),
    }
    changes = [
        chat_changes[conversation_id]
        for conversation_id in history["chat_delivery_order"]
    ] + [
        ("message-keep", _message_change("keep-message", "delete-keep", sent_days=-1, sent_seconds=0, direction=history["keep_direction"], text=TEXTS[0])),
        ("message-delete", _message_change("delete-message-only", "delete-message", sent_days=-1, sent_seconds=history["first_seconds"], direction=history["delete_direction"], text=TEXTS[1])),
        ("conversation-first", _message_change("delete-conversation-first", "delete-conversation", sent_days=-2, sent_seconds=history["first_seconds"], direction="inbound", text=TEXTS[2])),
        ("conversation-second", _message_change("delete-conversation-second", "delete-conversation", sent_days=-2, sent_seconds=history["second_seconds"], direction="outbound", text=TEXTS[3])),
        ("delete-message", _delete_message("delete-message-only", "delete-message")),
        ("delete-conversation", _delete_chat("delete-conversation")),
    ]
    trace.append(
        {
            "operation": "alternative_delivery_order",
            "outcome": "selected",
            "conversation_order": list(history["chat_delivery_order"]),
        }
    )
    latest: Any | None = None
    for index, (label, change) in enumerate(changes, start=1):
        _, latest = _apply(
            repositories,
            incremental,
            _payload(sequence, f"deletion-{label}", change),
            operation="canonical_delta",
            trace=trace,
        )
        if state is not None:
            state["artifact"] = latest
        sequence += 1
        if index == 8:
            pre_chat_delete = latest
            rebuilt, context = _clean_compare(
                repositories,
                incremental,
                latest,
                seed=history["seed"],
                trace=trace,
            )
            expected_semantics = expected_semantic_from_canonical(
                CREATOR_ID,
                history_source_for(repositories).account_read_model(CREATOR_ID),
                context,
            )
            if state is not None:
                state["clean_rebuild_count"] += 1
            for artifact in (latest, rebuilt):
                assert_deleted_material_absent(
                    artifact,
                    creator_account_id=CREATOR_ID,
                    expected_semantics=expected_semantics,
                    removed_messages=(("delete-message", "delete-message-only"),),
                    removed_participants=("fan-message",),
                )
        if index == 9:
            rebuilt, context = _clean_compare(
                repositories,
                incremental,
                latest,
                seed=history["seed"],
                trace=trace,
            )
            expected_semantics = expected_semantic_from_canonical(
                CREATOR_ID,
                history_source_for(repositories).account_read_model(CREATOR_ID),
                context,
            )
            if state is not None:
                state["clean_rebuild_count"] += 1
            for artifact in (latest, rebuilt):
                assert_deleted_material_absent(
                    artifact,
                    creator_account_id=CREATOR_ID,
                    expected_semantics=expected_semantics,
                    removed_messages=(
                        ("delete-message", "delete-message-only"),
                        ("delete-conversation", "delete-conversation-first"),
                        ("delete-conversation", "delete-conversation-second"),
                    ),
                    removed_conversations=("delete-conversation",),
                    removed_participants=("fan-message", "fan-last"),
                )
            incremental, latest = _restart_incremental_pipeline(
                repositories, trace=trace
            )
            if state is not None:
                state["pipeline"] = incremental
                state["artifact"] = latest
                state["repository_restart_count"] += 1

    assert latest is not None and pre_chat_delete is not None
    duplicate = _payload(8, "deletion-delete-message", changes[7][1])
    outcome, latest = _apply(
        repositories,
        incremental,
        duplicate,
        operation="duplicate_delete_replay",
        trace=trace,
    )
    assert outcome.status == "duplicate"
    assert trace[-1]["pipeline_changed"] is False
    if state is not None:
        state["artifact"] = latest
    reappearance = _message_change(
        "delete-message-only",
        "delete-message",
        sent_days=0,
        sent_seconds=0,
        direction="inbound",
        text="attempted reappearance",
    )
    outcome, latest = _apply(
        repositories,
        incremental,
        _payload(sequence, "deletion-stale-reappearance", reappearance),
        operation="tombstoned_reappearance",
        trace=trace,
    )
    assert outcome.status == "accepted" and outcome.canonical_revision is None
    assert trace[-1]["pipeline_changed"] is False
    if state is not None:
        state["artifact"] = latest
    rebuilt, context = _clean_compare(
        repositories,
        incremental,
        latest,
        seed=history["seed"],
        trace=trace,
    )
    expected_semantics = expected_semantic_from_canonical(
        CREATOR_ID,
        history_source_for(repositories).account_read_model(CREATOR_ID),
        context,
    )
    if state is not None:
        state["clean_rebuild_count"] += 1
    for artifact in (latest, rebuilt):
        assert_deleted_material_absent(
            artifact,
            creator_account_id=CREATOR_ID,
            expected_semantics=expected_semantics,
            removed_messages=(
                ("delete-message", "delete-message-only"),
                ("delete-conversation", "delete-conversation-first"),
                ("delete-conversation", "delete-conversation-second"),
            ),
            removed_conversations=("delete-conversation",),
            removed_participants=("fan-message", "fan-last"),
        )
    return latest, rebuilt, context, trace, pre_chat_delete, repositories


@pytest.mark.stateful_tier_a
class TestAnalyticsConvergence:
    @given(history=general_histories())
    def test_incremental_path_converges_with_fresh_rebuild(
        self, history: dict[str, Any]
    ) -> None:
        _run_generated_history(history, _run_general)


@pytest.mark.stateful_tier_a
class TestAnalyticsDeletionConvergence:
    @given(history=deletion_histories())
    def test_deletions_close_over_incremental_and_rebuilt_state(
        self, history: dict[str, Any]
    ) -> None:
        _run_generated_history(history, _run_deletion)


@pytest.mark.stateful_tier_a
def test_task6b_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults() -> None:
    """Every named negative control fails through the shared canonical oracle."""

    incremental, rebuilt, context, _, before_chat_delete, repositories = _run_deletion(
        {
            "chat_delivery_order": (
                "delete-message",
                "delete-keep",
                "delete-conversation",
            ),
            "keep_direction": "inbound",
            "delete_direction": "outbound",
            "first_seconds": 30,
            "second_seconds": 90,
            "seed": 90210,
        }
    )
    expected_semantics = expected_semantic_from_canonical(
        CREATOR_ID, history_source_for(repositories).account_read_model(CREATOR_ID), context
    )
    # The generated profiles additionally compare the full independent active
    # identity map.  These are direct semantic faults, so the context check
    # itself detects the provenance case before structural comparison.
    with pytest.raises(AnalyticsOracleMismatch, match="semantic_projection"):
        assert_incremental_rebuild_convergence(
            BrokenMetricAdapter.apply(incremental), rebuilt, context,
            expected_semantics=expected_semantics,
        )
    with pytest.raises(AnalyticsOracleMismatch, match="reproducibility context mismatch"):
        assert_incremental_rebuild_convergence(
            BrokenProvenanceAdapter.apply(incremental), rebuilt, context,
            expected_semantics=expected_semantics,
        )
    with pytest.raises(AnalyticsOracleMismatch, match="semantic_projection"):
        assert_incremental_rebuild_convergence(
            BrokenIdentityAdapter.apply(incremental), rebuilt, context,
            expected_semantics=expected_semantics,
        )
    with pytest.raises(AnalyticsOracleMismatch, match="graph digest mismatch"):
        assert_incremental_rebuild_convergence(
            BrokenGraphAdapter.apply(incremental), rebuilt, context,
            expected_semantics=expected_semantics,
        )
    final_node_ids = {node.node_id for node in incremental.nodes}
    stale_node = next(
        node
        for node in before_chat_delete.nodes
        if node.kind.value == "message" and node.node_id not in final_node_ids
    )
    with pytest.raises(AnalyticsOracleMismatch, match="referential closure"):
        assert_incremental_rebuild_convergence(
            BrokenDeletionClosureAdapter.apply(incremental, stale_node_id=stale_node.node_id),
            rebuilt,
            context,
            expected_semantics=expected_semantics,
        )
    with pytest.raises(AnalyticsOracleMismatch, match="canonical semantic mismatch"):
        assert_incremental_rebuild_convergence(
            BrokenDerivedDeletionAdapter.apply(incremental),
            BrokenDerivedDeletionAdapter.apply(rebuilt),
            context,
            expected_semantics=expected_semantics,
        )
    with pytest.raises(AnalyticsOracleMismatch, match="canonical semantic mismatch"):
        assert_incremental_rebuild_convergence(
            ForgedTopicEntityIdentityAdapter.apply(incremental),
            ForgedTopicEntityIdentityAdapter.apply(rebuilt),
            context,
            expected_semantics=expected_semantics,
        )


def _deletion_counterexample_case() -> tuple[Any, Any, ReproducibilityContext, dict[str, Any]]:
    incremental, rebuilt, context, _, _, repositories = _run_deletion(
        {
            "chat_delivery_order": (
                "delete-message",
                "delete-keep",
                "delete-conversation",
            ),
            "keep_direction": "inbound",
            "delete_direction": "outbound",
            "first_seconds": 30,
            "second_seconds": 90,
            "seed": 90212,
        }
    )
    return (
        incremental,
        rebuilt,
        context,
        expected_semantic_from_canonical(
            CREATOR_ID, history_source_for(repositories).account_read_model(CREATOR_ID), context
        ),
    )


@pytest.mark.stateful_tier_a
def test_shared_oracle_rejects_same_forged_topic_or_entity_graph_in_both_artifacts() -> None:
    """A valid, self-consistent forged graph cannot pass by agreement alone."""

    incremental, rebuilt, context, expected_semantics = _deletion_counterexample_case()
    with pytest.raises(AnalyticsOracleMismatch, match="canonical semantic mismatch"):
        assert_incremental_rebuild_convergence(
            ForgedTopicEntityIdentityAdapter.apply(incremental),
            ForgedTopicEntityIdentityAdapter.apply(rebuilt),
            context,
            expected_semantics=expected_semantics,
        )


@pytest.mark.stateful_tier_a
def test_shared_oracle_rejects_same_stale_deleted_message_metric_in_both_artifacts() -> None:
    """A structurally valid stale metric cannot pass by agreement alone."""

    incremental, rebuilt, context, expected_semantics = _deletion_counterexample_case()
    with pytest.raises(AnalyticsOracleMismatch, match="canonical semantic mismatch"):
        assert_incremental_rebuild_convergence(
            BrokenDerivedDeletionAdapter.apply(incremental),
            BrokenDerivedDeletionAdapter.apply(rebuilt),
            context,
            expected_semantics=expected_semantics,
        )


@pytest.mark.stateful_tier_a
def test_active_publication_oracle_rejects_stale_deleted_material_with_current_witness() -> None:
    """Current metadata cannot hide stale active projection or graph material."""

    state: dict[str, Any] = {}
    _run_deletion(
        {
            "chat_delivery_order": (
                "delete-message",
                "delete-keep",
                "delete-conversation",
            ),
            "keep_direction": "inbound",
            "delete_direction": "outbound",
            "first_seconds": 30,
            "second_seconds": 90,
            "seed": 90213,
        },
        state=state,
    )
    pipeline = state["pipeline"]
    account = pipeline.source.account_read_model(CREATOR_ID)
    context = _context(pipeline, seed=90213)
    expected_semantics = expected_semantic_from_canonical(
        CREATOR_ID, account, context
    )
    active, nodes, edges, graph_revision = _read_active_publication(pipeline, account)
    assert active is not None
    active_artifact = RebuildArtifact(projection=active, nodes=nodes, edges=edges)

    # This is the accepted adversarial case: a copied active projection keeps
    # its current witness while retaining a deleted-message metric contribution.
    stale_metric = BrokenDerivedDeletionAdapter.apply(active_artifact)
    with pytest.raises(AnalyticsOracleMismatch, match="canonical semantic mismatch"):
        assert_active_publication_witness(
            active_projection=stale_metric.projection,
            active_nodes=nodes,
            active_edges=edges,
            active_graph_revision=graph_revision,
            context=context,
            expected_semantics=expected_semantics,
        )

    # The graph variant remains referentially valid and has a recomputed graph
    # digest, so it verifies complete active node/edge material is also checked.
    stale_graph = ForgedTopicEntityIdentityAdapter.apply(active_artifact)
    with pytest.raises(AnalyticsOracleMismatch, match="active publication witness mismatch"):
        assert_active_publication_witness(
            active_projection=stale_graph.projection,
            active_nodes=stale_graph.nodes,
            active_edges=stale_graph.edges,
            active_graph_revision=graph_revision,
            context=context,
            expected_semantics=expected_semantics,
        )


@pytest.mark.stateful_tier_a
def test_deliberate_falsifier_failure_configures_hypothesis_shrink_phase() -> None:
    """Keep a measured, minimizable negative execution behind ``pytest.raises``."""

    incremental, rebuilt, context, _, _, repositories = _run_deletion(
        {
            "chat_delivery_order": (
                "delete-message",
                "delete-keep",
                "delete-conversation",
            ),
            "keep_direction": "inbound",
            "delete_direction": "outbound",
            "first_seconds": 31,
            "second_seconds": 91,
            "seed": 90211,
        }
    )
    expected_semantics = expected_semantic_from_canonical(
        CREATOR_ID, history_source_for(repositories).account_read_model(CREATOR_ID), context
    )

    generated_calls = 0

    @settings(
        max_examples=8,
        phases=(Phase.generate, Phase.shrink),
        deadline=None,
        database=None,
    )
    @given(marker=st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=8))
    def broken_property(marker: list[int]) -> None:
        nonlocal generated_calls
        generated_calls += 1
        # The generated marker supplies a JSON-safe minimization target while
        # the same independent convergence oracle observes the semantic fault.
        assert marker
        assert_incremental_rebuild_convergence(
            BrokenMetricAdapter.apply(incremental),
            rebuilt,
            context,
            expected_semantics=expected_semantics,
        )

    started = perf_counter()
    with pytest.raises(AnalyticsOracleMismatch):
        broken_property()
    _record_deliberate_falsifier(
        generated_calls=generated_calls,
        wall_clock_seconds=perf_counter() - started,
    )


def test_stale_candidate_is_rejected_and_active_publication_stays_current() -> None:
    """A real stale publication candidate cannot become active after a mutation."""

    repositories = create_canonical_repositories("memory")
    pipeline = AnalyticsPipeline(history_source_for(repositories), clock=lambda: EVALUATION_CLOCK)
    trace: list[dict[str, Any]] = []
    _initialize_canonical_stream(repositories, trace)
    _, _ = _apply(
        repositories,
        pipeline,
        _payload(1, "publication-chat", _chat_change("publication", "fan-publication", display_name="Initial", update_offset=-2)),
        operation="canonical_delta",
        trace=trace,
    )
    candidate = pipeline.build_candidate(CREATOR_ID)
    _, _ = _apply(
        repositories,
        pipeline,
        _payload(2, "publication-update", _chat_change("publication", "fan-publication", display_name="Changed", update_offset=-1)),
        operation="canonical_delta",
        trace=trace,
    )
    with pytest.raises(CanonicalRevisionChanged):
        pipeline.publish_candidate(candidate)
    pipeline.project_account(CREATOR_ID)
    context = _context(pipeline, seed=7)
    _assert_current_publication(
        pipeline,
        context,
        expected_semantic_from_canonical(
            CREATOR_ID,
            history_source_for(repositories).account_read_model(CREATOR_ID),
            context,
        ),
    )


def test_changed_analyzer_context_is_not_treated_as_equivalent() -> None:
    """A pipeline configuration change must produce distinct provenance."""

    repositories = create_canonical_repositories("memory")
    baseline = AnalyticsPipeline(history_source_for(repositories), clock=lambda: EVALUATION_CLOCK)
    trace: list[dict[str, Any]] = []
    _initialize_canonical_stream(repositories, trace)
    _apply(
        repositories,
        baseline,
        _payload(1, "config-chat", _chat_change("config", "fan-config", display_name="Config", update_offset=-1)),
        operation="canonical_delta",
        trace=trace,
    )
    _, baseline_artifact = _apply(
        repositories,
        baseline,
        _payload(2, "config-message", _message_change("config-message", "config", sent_days=-1, sent_seconds=0, direction="inbound", text=TEXTS[0])),
        operation="canonical_delta",
        trace=trace,
    )

    class AlternateSentiment(RuleBasedSentimentAnalyzer):
        revision = "sentiment.rules.task6b-config.v1"

    changed = AnalyticsPipeline(
        history_source_for(repositories),
        enrichment=EnrichmentStage(sentiment=AlternateSentiment()),
        clock=lambda: EVALUATION_CLOCK,
    )
    changed_artifact = changed.rebuild_account(CREATOR_ID).artifact
    baseline_context = _context(baseline, seed=8)
    changed_context = _context(changed, seed=8)
    assert changed_context.pipeline_config_digest != baseline_context.pipeline_config_digest
    with pytest.raises(AnalyticsOracleMismatch, match="reproducibility context mismatch"):
        assert_incremental_rebuild_convergence(
            baseline_artifact, changed_artifact, baseline_context
        )


def test_profile_calibration_and_oracle_independence_are_explicit() -> None:
    assert settings.get_profile("task6b_convergence_fast").max_examples == 6
    assert settings.get_profile("task6b_convergence_stress").max_examples == 30
    assert settings.get_profile("task6_deletion_fast").max_examples == 4
    assert settings.get_profile("task6_deletion_stress").max_examples == 20
    source = Path(__file__).parents[1] / "state_models" / "analytics_oracle.py"
    imports = {
        node.module
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not imports & {
        "app.analytics.canonical_source",
        "app.analytics.enrichment",
        "app.analytics.graph_projection",
        "app.analytics.identity",
        "app.analytics.opaque_refs",
        "app.analytics.graph_identity",
        "app.analytics.graph_privacy",
        "app.analytics.metrics",
        "app.analytics.pipeline",
        "app.analytics.projection_store",
        "app.analytics.provenance",
    }
