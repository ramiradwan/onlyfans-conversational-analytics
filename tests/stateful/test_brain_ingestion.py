"""Tier A model-based assurance for the authoritative HistoryRepository seam.

The repository-level catalogue contains 42 entries: D01-D09, A01-A14, and
N01-N12/N14-N20. S01-S03 belong to transport admission and N13 belongs to
protocol parsing, so this suite does not claim them.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, fields, is_dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from app.persistence.factory import create_canonical_repositories
from app.persistence.history import HistoryRepository
from tests.state_models.brain_ingestion_model import (
    ModelChatDeleteCommand,
    ModelChatUpsertCommand,
    ModelCoverageObservedCommand,
    ModelMessageDeleteCommand,
    ModelMessageUpsertCommand,
    ModelSnapshotBeginCommand,
    ModelSnapshotChunkCommand,
    ModelSnapshotCommitCommand,
    ModelStreamKey,
    PureBrainIngestionModel,
)
from tests.state_models.production_brain_adapter import ProductionBrainAdapter
from tests.state_models.transition_oracle import assert_transition_oracle


for profile, examples, steps in (
    ("tier_a_general", 15, 20),
    ("tier_a_deletion", 10, 20),
    ("dev", 8, 12),
    ("smoke", 3, 8),
):
    settings.register_profile(
        profile,
        max_examples=examples,
        stateful_step_count=steps,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

CATALOGUE_IDS = frozenset(
    [f"D{i:02d}" for i in range(1, 10)]
    + [f"A{i:02d}" for i in range(1, 15)]
    + [f"N{i:02d}" for i in range(1, 21) if i != 13]
)
CATALOGUE_SCENARIOS: dict[str, frozenset[str]] = {
    "exercise_deltas": frozenset(f"D{i:02d}" for i in range(1, 10)),
    "exercise_anomalies": frozenset(f"A{i:02d}" for i in range(1, 15)),
    "exercise_snapshots": frozenset(
        [f"N{i:02d}" for i in range(1, 13)]
        + [f"N{i:02d}" for i in range(14, 21)]
    ),
}


def stable_uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ofca-task5a/{label}")


def chat_record(chat_id: str, *, name: str | None = None) -> dict[str, Any]:
    return {
        "tombstone": False,
        "chat": {
            "record_kind": "full",
            "chat_id": chat_id,
            "platform_user_id": f"fan-{chat_id}",
            "display_name": name or chat_id,
            "updated_at": "2026-07-19T10:00:00+00:00",
        },
    }


def message_record(message_id: str, chat_id: str) -> dict[str, Any]:
    return {
        "tombstone": False,
        "message": {
            "message_id": message_id,
            "chat_id": chat_id,
            "sender_platform_user_id": f"fan-{chat_id}",
            "text": message_id,
            "sent_at": "2026-07-19T10:01:00+00:00",
            "direction": "inbound",
        },
    }


def _trace_value(value: Any) -> Any:
    """Return a deterministic, JSON-serializable command representation."""
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value):
        return {field.name: _trace_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _trace_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_trace_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_trace_value(item) for item in value)
    return value


def _trace_key(key: ModelStreamKey) -> dict[str, str]:
    return {
        "creator_account_id": key.creator_account_id,
        "agent_installation_id": str(key.agent_installation_id),
        "agent_stream_id": str(key.agent_stream_id),
    }


@dataclass
class Harness:
    """One deterministic model/production pair with a single ingest path."""

    label: str

    def __post_init__(self) -> None:
        self.account = f"task5a-{self.label}"
        self.key = self.stream("main")
        self.repos = create_canonical_repositories("memory")
        self.history = self.repos.history
        self.database = self.repos.database
        self.model = PureBrainIngestionModel(self.account)
        self.adapter = ProductionBrainAdapter(
            self.history,
            self.database,
            connection_id=stable_uuid(f"{self.label}/connection"),
        )
        self.counter = 0
        self.trace: list[dict[str, Any]] = []
        self.coverage_configured = False
        self.seed_stream(self.key, "base")

    def new_id(self, purpose: str) -> UUID:
        self.counter += 1
        return stable_uuid(f"{self.label}/{purpose}/{self.counter}")

    def stream(self, name: str) -> ModelStreamKey:
        return ModelStreamKey(
            self.account,
            stable_uuid(f"{self.label}/{name}/installation"),
            stable_uuid(f"{self.label}/{name}/stream"),
        )

    def next_seq(self, key: ModelStreamKey | None = None) -> int:
        checkpoint = self.model.checkpoint(key or self.key)
        return (0 if checkpoint is None else checkpoint) + 1

    def frame(
        self,
        command: Any,
        operation: str,
        *requirements: str,
        key: ModelStreamKey | None = None,
    ) -> Any:
        """Execute and compare every model and production ingest transition."""
        target = key or self.key
        before = self.adapter.observe_state(target)
        model_outcome = getattr(self.model, operation)(target, command)
        production_outcome = getattr(self.adapter, operation)(target, command)
        after = self.adapter.observe_state(target)
        self.trace.append(
            {
                "operation": operation,
                "stream_key": _trace_key(target),
                "command": _trace_value(command),
                "requirements": sorted(requirements),
            }
        )
        assert_transition_oracle(
            model=self.model,
            adapter=self.adapter,
            key=target,
            command=command,
            model_outcome=model_outcome,
            prod_outcome=production_outcome,
            snapshot_before=before,
            snapshot_after=after,
            command_history=self.trace,
        )
        return production_outcome

    def chat(
        self,
        chat_id: str,
        *,
        key: ModelStreamKey | None = None,
        source_seq: int | None = None,
        event_id: UUID | None = None,
        kind: str = "full",
        fan: str | None = None,
        name: str | None = None,
        updated_at: str = "2026-07-19T10:00:00+00:00",
    ) -> ModelChatUpsertCommand:
        target = key or self.key
        return ModelChatUpsertCommand(
            event_id or self.new_id("event"),
            self.next_seq(target) if source_seq is None else source_seq,
            chat_id,
            kind,
            (f"fan-{chat_id}" if fan is None and kind == "full" else fan),
            name or chat_id,
            updated_at if kind == "full" else None,
        )

    def message(
        self,
        message_id: str,
        chat_id: str,
        *,
        key: ModelStreamKey | None = None,
        source_seq: int | None = None,
        event_id: UUID | None = None,
        text: str | None = None,
    ) -> ModelMessageUpsertCommand:
        target = key or self.key
        return ModelMessageUpsertCommand(
            event_id or self.new_id("event"),
            self.next_seq(target) if source_seq is None else source_seq,
            message_id,
            chat_id,
            f"fan-{chat_id}",
            text or message_id,
            "2026-07-19T10:01:00+00:00",
            "inbound",
        )

    def seed_stream(self, key: ModelStreamKey, chat_id: str, through_seq: int = 1) -> None:
        snapshot_id = self.new_id("seed")
        self.frame(
            ModelSnapshotBeginCommand(snapshot_id, through_seq, 1, 1, 0, 0),
            "begin_snapshot",
            key=key,
        )
        self.frame(
            ModelSnapshotChunkCommand(snapshot_id, 0, "chat", [chat_record(chat_id)]),
            "add_snapshot_chunk",
            key=key,
        )
        self.frame(ModelSnapshotCommitCommand(snapshot_id, 1), "commit_snapshot", key=key)

    def configure_coverage(self) -> None:
        if self.coverage_configured:
            return
        values = {
            "consent_policy_version": "history-consent-v1",
            "consent_revision": "consent-1",
            "authorized_platform_creator_id": "creator",
            "desired_state": "running",
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        }
        saved = self.history.update_history_settings(
            self.account, expected_revision=0, values=values
        )
        self.history.bind_history_config(
            self.account,
            settings_revision=int(saved["settings_revision"]),
            config_revision="task5a-coverage",
        )
        self.history.mark_history_config_applied(self.account, "task5a-coverage")
        self.coverage_configured = True
        self.trace.append(
            {
                "operation": "configure_coverage",
                "stream_key": _trace_key(self.key),
                "command": _trace_value(values),
                "requirements": [],
            }
        )

    def reconstruct_repository(self, *, key: ModelStreamKey | None = None) -> None:
        target = key or self.key
        expected = self.adapter.observe_state(target)
        self.history = HistoryRepository(self.database)
        self.adapter = ProductionBrainAdapter(
            self.history,
            self.database,
            connection_id=self.new_id("reconstruction"),
        )
        assert self.adapter.observe_state(target) == expected
        self.trace.append(
            {
                "operation": "reconstruct_repository",
                "stream_key": _trace_key(target),
                "command": {},
                "requirements": [],
            }
        )

    def exercise_deltas(self) -> None:
        self.frame(self.chat("d1"), "commit_delta", "D01")
        self.frame(self.chat("d1", name="newer", updated_at="2026-07-19T11:00:00+00:00"), "commit_delta", "D04")
        self.frame(self.chat("d2", kind="placeholder"), "commit_delta", "D02")
        self.frame(self.chat("d2", name="promoted"), "commit_delta", "D02")
        self.frame(self.chat("d2", kind="placeholder"), "commit_delta", "D03")
        self.frame(self.chat("d2", name="stale", updated_at="2026-07-19T09:00:00+00:00"), "commit_delta", "D05")
        self.frame(self.message("d6", "d1"), "commit_delta", "D06")
        self.frame(ModelMessageDeleteCommand(self.new_id("event"), self.next_seq(), "d6", "d1"), "commit_delta", "D08")
        self.frame(self.message("d7-live", "d1"), "commit_delta", "D07")
        self.frame(ModelChatDeleteCommand(self.new_id("event"), self.next_seq(), "d1"), "commit_delta", "D07")
        self.configure_coverage()
        generation = self.new_id("generation")
        evidence = [("generation.started", {"generation_id": generation, "as_of": "2026-07-19T12:00:00Z", "authorization_revision": "consent-1"}),
                    ("inventory.member", {"generation_id": generation, "conversation_id": "base"}),
                    ("inventory.ended", {"generation_id": generation, "observed_at": "2026-07-19T12:01:00Z"}),
                    ("conversation.history_started", {"generation_id": generation, "conversation_id": "base", "earliest_observed_at": None, "observed_at": "2026-07-19T12:02:00Z"}),
                    ("conversation.head_reconciled", {"generation_id": generation, "conversation_id": "base", "reconciled_through": "2026-07-19T12:00:00Z"}),
                    ("generation.closed", {"generation_id": generation, "closed_at": "2026-07-19T12:03:00Z"})]
        for kind, payload in evidence:
            self.frame(ModelCoverageObservedCommand(self.new_id("event"), self.next_seq(), kind, payload), "commit_delta", "D09")

    def _recover(self, suffix: str, requirement: str) -> None:
        assert self.frame(self.chat(f"recover-{suffix}"), "commit_delta", requirement).disposition == "accepted"

    def exercise_anomalies(self) -> None:
        exact = self.chat("a1"); self.frame(exact, "commit_delta"); self.frame(exact, "commit_delta", "A01")
        original = self.chat("a2"); self.frame(original, "commit_delta")
        self.frame(self.chat("a2-conflict", event_id=original.event_id), "commit_delta", "A02"); self._recover("a2", "A02")
        original = self.chat("a3"); self.frame(original, "commit_delta")
        self.frame(self.chat("a3-reuse", source_seq=original.source_seq), "commit_delta", "A03"); self._recover("a3", "A03")
        old_key = self.stream("old-replay"); self.seed_stream(old_key, "old-base", through_seq=5)
        self.frame(self.chat("old", key=old_key, source_seq=2), "commit_delta", "A04", key=old_key)
        self.frame(self.chat("gap", source_seq=self.next_seq() + 1), "commit_delta", "A05"); self._recover("a5", "A05")
        unstarted = self.stream("unstarted")
        self.frame(self.chat("unstarted", key=unstarted, source_seq=1), "commit_delta", "A06", key=unstarted)
        message = self.message("a7-message", "base"); self.frame(message, "commit_delta")
        self.frame(self.message("a7-message", "base", text="changed"), "commit_delta", "A07"); self._recover("a7", "A07")
        self.frame(self.chat("base", fan="different"), "commit_delta", "A08"); self._recover("a8", "A08")
        self.frame(self.chat("base", name="same-time-conflict", updated_at="2026-07-19T10:00:00+00:00"), "commit_delta", "A09"); self._recover("a9", "A09")
        self.frame(self.message("orphan", "missing"), "commit_delta", "A10"); self._recover("a10", "A10")
        self.frame(ModelMessageDeleteCommand(self.new_id("event"), self.next_seq(), "a7-message", "wrong-chat"), "commit_delta", "A11"); self._recover("a11", "A11")
        self.frame(ModelMessageDeleteCommand(self.new_id("event"), self.next_seq(), "unknown-message", "base"), "commit_delta")
        self.frame(ModelMessageDeleteCommand(self.new_id("event"), self.next_seq(), "unknown-message", "other-chat"), "commit_delta", "A12"); self._recover("a12", "A12")
        self.frame(ModelChatDeleteCommand(self.new_id("event"), self.next_seq(), "base"), "commit_delta")
        self.frame(self.chat("base"), "commit_delta", "A13")
        self.frame(self.chat("a14-chat"), "commit_delta"); self.frame(self.message("a14-message", "a14-chat"), "commit_delta")
        self.frame(ModelMessageDeleteCommand(self.new_id("event"), self.next_seq(), "a14-message", "a14-chat"), "commit_delta")
        self.frame(self.message("a14-message", "a14-chat"), "commit_delta", "A14")

    def exercise_snapshots(self) -> None:
        lifecycle_key = self.stream("lifecycle"); snapshot_id = self.new_id("snapshot")
        begin = ModelSnapshotBeginCommand(snapshot_id, 1, 1, 1, 0, 0)
        self.frame(begin, "begin_snapshot", "N01", key=lifecycle_key); self.frame(begin, "begin_snapshot", "N02", key=lifecycle_key)
        self.frame(ModelSnapshotBeginCommand(snapshot_id, 1, 2, 2, 0, 0), "begin_snapshot", "N03", key=lifecycle_key)
        self.frame(ModelSnapshotBeginCommand(self.new_id("parallel"), 1, 0, 0, 0, 0), "begin_snapshot", "N04", key=lifecycle_key)
        chunk = ModelSnapshotChunkCommand(snapshot_id, 0, "chat", [chat_record("lifecycle-chat")])
        self.frame(chunk, "add_snapshot_chunk", "N06", "N19", key=lifecycle_key); self.frame(chunk, "add_snapshot_chunk", "N07", key=lifecycle_key)
        # A permitted in-memory repository reconstruction cut point preserves
        # staged metadata and is observed before the later commit transition.
        self.reconstruct_repository()
        self.frame(ModelSnapshotChunkCommand(snapshot_id, 0, "chat", [chat_record("lifecycle-conflict")]), "add_snapshot_chunk", "N08", key=lifecycle_key)
        self.frame(ModelSnapshotCommitCommand(snapshot_id, 2), "commit_snapshot", "N17", key=lifecycle_key)
        commit = ModelSnapshotCommitCommand(snapshot_id, 1)
        self.frame(commit, "commit_snapshot", "N14", key=lifecycle_key); self.frame(commit, "commit_snapshot", "N15", key=lifecycle_key)

        zero_key = self.stream("zero"); zero = self.new_id("zero")
        self.frame(ModelSnapshotBeginCommand(zero, 0, 0, 0, 0, 0), "begin_snapshot", "N01", key=zero_key)
        self.frame(ModelSnapshotCommitCommand(zero, 0), "commit_snapshot", "N14", key=zero_key)

        self.configure_coverage(); multi_key = self.stream("multi"); multi = self.new_id("multi"); generation = self.new_id("snapshot-generation")
        self.frame(ModelSnapshotBeginCommand(multi, 1, 3, 1, 1, 1), "begin_snapshot", key=multi_key)
        self.frame(ModelSnapshotChunkCommand(multi, 1, "message", [message_record("multi-message", "multi-chat")]), "add_snapshot_chunk", "N09", key=multi_key)
        self.frame(ModelSnapshotChunkCommand(multi, 0, "chat", [chat_record("multi-chat")]), "add_snapshot_chunk", "N06", key=multi_key)
        self.frame(ModelSnapshotChunkCommand(multi, 1, "message", [message_record("multi-message", "multi-chat")]), "add_snapshot_chunk", "N06", key=multi_key)
        self.frame(ModelSnapshotChunkCommand(multi, 2, "coverage_evidence", [{"type":"generation.started","generation_id":str(generation),"as_of":"2026-07-19T12:00:00Z","authorization_revision":"consent-1"}]), "add_snapshot_chunk", "N06", key=multi_key)
        self.frame(ModelSnapshotCommitCommand(multi, 3), "commit_snapshot", "N14", key=multi_key)

        behind_key = self.stream("behind"); self.seed_stream(behind_key, "behind-base", through_seq=5); behind = self.new_id("behind")
        self.frame(ModelSnapshotBeginCommand(behind, 2, 0, 0, 0, 0), "begin_snapshot", key=behind_key)
        self.frame(ModelSnapshotCommitCommand(behind, 0), "commit_snapshot", "N05", key=behind_key)

        order_key = self.stream("order"); order = self.new_id("order")
        self.frame(ModelSnapshotBeginCommand(order, 1, 2, 1, 1, 0), "begin_snapshot", key=order_key)
        self.frame(ModelSnapshotChunkCommand(order, 0, "message", [message_record("order-message", "base")]), "add_snapshot_chunk", key=order_key)
        self.frame(ModelSnapshotChunkCommand(order, 1, "chat", [chat_record("order-chat")]), "add_snapshot_chunk", "N10", key=order_key)

        missing_key = self.stream("missing")
        self.frame(ModelSnapshotChunkCommand(self.new_id("missing"), 0, "chat", [chat_record("missing-chat")]), "add_snapshot_chunk", "N11", key=missing_key)

        oversized_key = self.stream("oversized"); oversized = self.new_id("oversized")
        self.frame(ModelSnapshotBeginCommand(oversized, 1, 1, 3, 0, 0), "begin_snapshot", key=oversized_key)
        large_records = [chat_record(f"large-{index}", name="x" * 180_000) for index in range(3)]
        self.frame(ModelSnapshotChunkCommand(oversized, 0, "chat", large_records, estimated_bytes=540_000), "add_snapshot_chunk", "N12", key=oversized_key)

        incomplete_key = self.stream("incomplete"); incomplete = self.new_id("incomplete")
        self.frame(ModelSnapshotBeginCommand(incomplete, 1, 2, 1, 1, 0), "begin_snapshot", key=incomplete_key)
        self.frame(ModelSnapshotChunkCommand(incomplete, 0, "chat", [chat_record("incomplete-chat")]), "add_snapshot_chunk", key=incomplete_key)
        self.frame(ModelSnapshotCommitCommand(incomplete, 2), "commit_snapshot", "N16", key=incomplete_key)

        race_key = self.stream("race"); self.seed_stream(race_key, "race-base"); race = self.new_id("race")
        self.frame(ModelSnapshotBeginCommand(race, 5, 1, 1, 0, 0), "begin_snapshot", key=race_key)
        self.frame(ModelSnapshotChunkCommand(race, 0, "chat", [chat_record("race-snapshot")]), "add_snapshot_chunk", key=race_key)
        self.frame(self.chat("race-delta", key=race_key), "commit_delta", key=race_key)
        self.frame(ModelSnapshotCommitCommand(race, 1), "commit_snapshot", "N18", key=race_key)

        tombstone_key = self.stream("tombstone"); self.seed_stream(tombstone_key, "tombstone-chat")
        self.frame(ModelChatDeleteCommand(self.new_id("event"), self.next_seq(tombstone_key), "tombstone-chat"), "commit_delta", key=tombstone_key)
        resurrection = self.new_id("resurrection")
        self.frame(ModelSnapshotBeginCommand(resurrection, 3, 1, 1, 0, 0), "begin_snapshot", key=tombstone_key)
        self.frame(ModelSnapshotChunkCommand(resurrection, 0, "chat", [chat_record("tombstone-chat", name="revived")]), "add_snapshot_chunk", key=tombstone_key)
        self.frame(ModelSnapshotCommitCommand(resurrection, 1), "commit_snapshot", "N20", key=tombstone_key)


class BrainIngestionStateMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.harness = Harness("generated-general")
        self.old_replay_key = self.harness.stream("generated-old-replay")
        self.harness.seed_stream(self.old_replay_key, "generated-old-base", through_seq=4)
        self.immutable_message_id: str | None = None
        self.coverage_generation: UUID | None = None
        self.coverage_step = 0
        self.pending_snapshot_id: UUID | None = None
        self.snapshot_stage = 0
        self.snapshot_key = self.harness.stream("generated-staged")
        self.snapshot_chat: ModelSnapshotChunkCommand | None = None
        self.snapshot_message: ModelSnapshotChunkCommand | None = None
        self.order_snapshot_id: UUID | None = None
        self.order_stage = 0
        self.order_key = self.harness.stream("generated-order")

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def contiguous_chat(self, value: int) -> None:
        self.harness.frame(self.harness.chat(f"generated-chat-{value}-{self.harness.counter}"), "commit_delta", "D01")

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def message_then_delete(self, value: int) -> None:
        suffix = f"{value}-{self.harness.counter}"; chat_id = f"generated-parent-{suffix}"; message_id = f"generated-message-{suffix}"
        self.harness.frame(self.harness.chat(chat_id), "commit_delta", "D01")
        self.harness.frame(self.harness.message(message_id, chat_id), "commit_delta", "D06")
        self.harness.frame(ModelMessageDeleteCommand(self.harness.new_id("event"), self.harness.next_seq(), message_id, chat_id), "commit_delta", "D08")

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def gap_then_recovery(self, value: int) -> None:
        before = self.harness.model.checkpoint(self.harness.key) or 0
        self.harness.frame(self.harness.chat(f"generated-gap-{value}", source_seq=before + 2), "commit_delta", "A05")
        assert self.harness.frame(self.harness.chat(f"generated-recovery-{value}-{self.harness.counter}"), "commit_delta", "A05").disposition == "accepted"

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def duplicate_delivery(self, value: int) -> None:
        command = self.harness.chat(f"generated-duplicate-{value}-{self.harness.counter}")
        self.harness.frame(command, "commit_delta"); self.harness.frame(command, "commit_delta", "A01")

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def rejection_then_recovery(self, value: int) -> None:
        self.harness.frame(self.harness.message(f"generated-orphan-{value}-{self.harness.counter}", "missing-parent"), "commit_delta", "A10")
        assert self.harness.frame(self.harness.chat(f"generated-after-reject-{value}-{self.harness.counter}"), "commit_delta", "A10").disposition == "accepted"

    @rule()
    def old_sequence_replay(self) -> None:
        """Replay a historical sequence on a stream whose checkpoint is ahead."""
        self.harness.frame(
            self.harness.chat("generated-old-replay", key=self.old_replay_key, source_seq=2),
            "commit_delta",
            "A04",
            key=self.old_replay_key,
        )

    @precondition(lambda self: self.immutable_message_id is None)
    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def establish_immutable_message(self, value: int) -> None:
        self.immutable_message_id = f"generated-immutable-{value}-{self.harness.counter}"
        self.harness.frame(
            self.harness.message(self.immutable_message_id, "base", text="original"),
            "commit_delta",
            "D06",
        )

    @precondition(lambda self: self.immutable_message_id is not None)
    @rule()
    def immutable_message_conflict(self) -> None:
        assert self.immutable_message_id is not None
        self.harness.frame(
            self.harness.message(self.immutable_message_id, "base", text="changed"),
            "commit_delta",
            "A07",
        )

    @precondition(lambda self: self.coverage_step == 0)
    @rule()
    def coverage_generation_started(self) -> None:
        self.harness.configure_coverage()
        self.coverage_generation = self.harness.new_id("generated-coverage")
        self.harness.frame(
            ModelCoverageObservedCommand(
                self.harness.new_id("event"), self.harness.next_seq(), "generation.started",
                {"generation_id": self.coverage_generation, "as_of": "2026-07-19T12:00:00Z", "authorization_revision": "consent-1"},
            ),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 1

    @precondition(lambda self: self.coverage_step == 1)
    @rule()
    def coverage_inventory_member(self) -> None:
        self.harness.frame(
            ModelCoverageObservedCommand(self.harness.new_id("event"), self.harness.next_seq(), "inventory.member", {"generation_id": self.coverage_generation, "conversation_id": "base"}),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 2

    @precondition(lambda self: self.coverage_step == 2)
    @rule()
    def coverage_inventory_ended(self) -> None:
        self.harness.frame(
            ModelCoverageObservedCommand(self.harness.new_id("event"), self.harness.next_seq(), "inventory.ended", {"generation_id": self.coverage_generation, "observed_at": "2026-07-19T12:01:00Z"}),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 3

    @precondition(lambda self: self.coverage_step == 3)
    @rule()
    def coverage_history_started(self) -> None:
        self.harness.frame(
            ModelCoverageObservedCommand(self.harness.new_id("event"), self.harness.next_seq(), "conversation.history_started", {"generation_id": self.coverage_generation, "conversation_id": "base", "earliest_observed_at": None, "observed_at": "2026-07-19T12:02:00Z"}),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 4

    @precondition(lambda self: self.coverage_step == 4)
    @rule()
    def coverage_head_reconciled(self) -> None:
        self.harness.frame(
            ModelCoverageObservedCommand(self.harness.new_id("event"), self.harness.next_seq(), "conversation.head_reconciled", {"generation_id": self.coverage_generation, "conversation_id": "base", "reconciled_through": "2026-07-19T12:00:00Z"}),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 5

    @precondition(lambda self: self.coverage_step == 5)
    @rule()
    def coverage_generation_closed(self) -> None:
        self.harness.frame(
            ModelCoverageObservedCommand(self.harness.new_id("event"), self.harness.next_seq(), "generation.closed", {"generation_id": self.coverage_generation, "closed_at": "2026-07-19T12:03:00Z"}),
            "commit_delta",
            "D09",
        )
        self.coverage_step = 6

    @precondition(lambda self: self.pending_snapshot_id is None)
    @rule()
    def begin_staged_snapshot(self) -> None:
        self.pending_snapshot_id = self.harness.new_id("generated-staged-snapshot")
        self.harness.frame(
            ModelSnapshotBeginCommand(self.pending_snapshot_id, 1, 2, 1, 1, 0),
            "begin_snapshot",
            "N01",
            key=self.snapshot_key,
        )
        self.snapshot_stage = 0

    @precondition(lambda self: self.pending_snapshot_id is not None)
    @rule()
    def duplicate_staged_snapshot_begin(self) -> None:
        assert self.pending_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotBeginCommand(self.pending_snapshot_id, 1, 2, 1, 1, 0),
            "begin_snapshot",
            "N02",
            key=self.snapshot_key,
        )

    @precondition(lambda self: self.pending_snapshot_id is not None)
    @rule()
    def conflicting_staged_snapshot_begin(self) -> None:
        assert self.pending_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotBeginCommand(self.pending_snapshot_id, 1, 3, 2, 1, 0),
            "begin_snapshot",
            "N03",
            key=self.snapshot_key,
        )

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 0)
    @rule()
    def skipped_staged_snapshot_chunk(self) -> None:
        assert self.pending_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotChunkCommand(self.pending_snapshot_id, 1, "message", [message_record("generated-skipped", "base")]),
            "add_snapshot_chunk",
            "N09",
            key=self.snapshot_key,
        )

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 0)
    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def staged_snapshot_chat_chunk(self, value: int) -> None:
        assert self.pending_snapshot_id is not None
        self.snapshot_chat = ModelSnapshotChunkCommand(
            self.pending_snapshot_id, 0, "chat", [chat_record(f"generated-staged-{value}")]
        )
        self.harness.frame(self.snapshot_chat, "add_snapshot_chunk", "N06", key=self.snapshot_key)
        self.snapshot_stage = 1

    @precondition(lambda self: self.snapshot_chat is not None and self.snapshot_stage == 1)
    @rule()
    def duplicate_staged_snapshot_chunk(self) -> None:
        assert self.snapshot_chat is not None
        self.harness.frame(self.snapshot_chat, "add_snapshot_chunk", "N07", key=self.snapshot_key)

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 1)
    @rule()
    def conflicting_staged_snapshot_chunk(self) -> None:
        assert self.pending_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotChunkCommand(self.pending_snapshot_id, 0, "chat", [chat_record("generated-conflicting")]),
            "add_snapshot_chunk",
            "N08",
            key=self.snapshot_key,
        )

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 1)
    @rule()
    def reconstruct_while_snapshot_staged(self) -> None:
        self.harness.reconstruct_repository(key=self.snapshot_key)

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 1)
    @rule()
    def staged_snapshot_message_chunk(self) -> None:
        assert self.pending_snapshot_id is not None
        assert self.snapshot_chat is not None
        chat_id = self.snapshot_chat.records[0]["chat"]["chat_id"]
        self.snapshot_message = ModelSnapshotChunkCommand(
            self.pending_snapshot_id, 1, "message", [message_record(f"{chat_id}-message", chat_id)]
        )
        self.harness.frame(self.snapshot_message, "add_snapshot_chunk", "N06", key=self.snapshot_key)
        self.snapshot_stage = 2

    @precondition(lambda self: self.pending_snapshot_id is not None and self.snapshot_stage == 2)
    @rule()
    def commit_staged_snapshot(self) -> None:
        assert self.pending_snapshot_id is not None
        self.harness.frame(ModelSnapshotCommitCommand(self.pending_snapshot_id, 2), "commit_snapshot", "N14", key=self.snapshot_key)
        self.pending_snapshot_id = None
        self.snapshot_chat = None
        self.snapshot_message = None
        self.snapshot_stage = 0

    @precondition(lambda self: self.order_snapshot_id is None)
    @rule()
    def begin_ordering_snapshot(self) -> None:
        self.order_snapshot_id = self.harness.new_id("generated-order-snapshot")
        self.harness.frame(
            ModelSnapshotBeginCommand(self.order_snapshot_id, 1, 2, 1, 1, 0),
            "begin_snapshot",
            key=self.order_key,
        )
        self.order_stage = 0

    @precondition(lambda self: self.order_snapshot_id is not None and self.order_stage == 0)
    @rule()
    def ordering_message_first(self) -> None:
        assert self.order_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotChunkCommand(self.order_snapshot_id, 0, "message", [message_record("generated-order-message", "base")]),
            "add_snapshot_chunk",
            key=self.order_key,
        )
        self.order_stage = 1

    @precondition(lambda self: self.order_snapshot_id is not None and self.order_stage == 1)
    @rule()
    def ordering_chat_after_message(self) -> None:
        assert self.order_snapshot_id is not None
        self.harness.frame(
            ModelSnapshotChunkCommand(self.order_snapshot_id, 1, "chat", [chat_record("generated-order-chat")]),
            "add_snapshot_chunk",
            "N10",
            key=self.order_key,
        )

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def generated_snapshot_chat_conflicts(self, value: int) -> None:
        """Generated commit-time identity and equal-version conflict coverage."""
        key = self.harness.stream(f"generated-platform-{value}-{self.harness.counter}")
        snapshot = self.harness.new_id("generated-platform-snapshot")
        record = chat_record("base")
        record["chat"]["platform_user_id"] = f"different-{value}"
        self.harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=key)
        self.harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [record]), "add_snapshot_chunk", key=key)
        self.harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", "A08", key=key)

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def generated_snapshot_orphan_message(self, value: int) -> None:
        key = self.harness.stream(f"generated-orphan-snapshot-{value}-{self.harness.counter}")
        snapshot = self.harness.new_id("generated-orphan-snapshot")
        self.harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 0, 1, 0), "begin_snapshot", key=key)
        self.harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "message", [message_record(f"generated-orphan-snapshot-{value}", "missing-parent")]), "add_snapshot_chunk", key=key)
        self.harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", "A10", key=key)

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def generated_snapshot_chat_tombstone_cascade(self, value: int) -> None:
        chat_id = f"generated-cascade-{value}-{self.harness.counter}"
        message_id = f"{chat_id}-message"
        self.harness.frame(self.harness.chat(chat_id), "commit_delta", "D01")
        self.harness.frame(self.harness.message(message_id, chat_id), "commit_delta", "D06")
        key = self.harness.stream(f"generated-cascade-snapshot-{value}-{self.harness.counter}")
        snapshot = self.harness.new_id("generated-cascade-snapshot")
        self.harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=key)
        self.harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [{"tombstone": True, "chat_id": chat_id}]), "add_snapshot_chunk", key=key)
        self.harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", "D07", key=key)

    @rule(value=st.integers(min_value=0, max_value=1_000_000))
    def zero_or_single_snapshot(self, value: int) -> None:
        snapshot_id = self.harness.new_id("generated-snapshot")
        count = value % 2
        self.harness.frame(ModelSnapshotBeginCommand(snapshot_id, self.harness.next_seq(), count, count, 0, 0), "begin_snapshot", "N01")
        if count:
            chat_id = f"generated-snapshot-chat-{value}-{self.harness.counter}"
            self.harness.frame(ModelSnapshotChunkCommand(snapshot_id, 0, "chat", [chat_record(chat_id)]), "add_snapshot_chunk", "N06", "N19")
        self.harness.frame(ModelSnapshotCommitCommand(snapshot_id, count), "commit_snapshot", "N14")


class BrainIngestionDeletionStateMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.harness = Harness("generated-deletion")
        self.message_id = "deleted-message"
        self.harness.frame(self.harness.message(self.message_id, "base"), "commit_delta", "D06")
        self.message_delete = ModelMessageDeleteCommand(self.harness.new_id("event"), self.harness.next_seq(), self.message_id, "base")
        self.harness.frame(self.message_delete, "commit_delta", "D08")
        self.chat_delete = ModelChatDeleteCommand(self.harness.new_id("event"), self.harness.next_seq(), "base")
        self.harness.frame(self.chat_delete, "commit_delta", "D07")
        self.fresh_stream = self.harness.stream("deletion-fresh")
        self.fresh_snapshot_id: UUID | None = None
        self.fresh_snapshot_stage = 0

    @rule()
    def stale_chat_replay(self) -> None:
        self.harness.frame(self.harness.chat("base"), "commit_delta", "A13")

    @rule()
    def stale_message_replay(self) -> None:
        self.harness.frame(self.harness.message(self.message_id, "base"), "commit_delta", "A14")

    @rule()
    def duplicate_delete_retry(self) -> None:
        self.harness.frame(self.chat_delete, "commit_delta", "A01")

    @rule()
    @precondition(lambda self: self.fresh_snapshot_id is None)
    def fresh_stream_snapshot_begin(self) -> None:
        self.fresh_snapshot_id = self.harness.new_id("deletion-fresh-snapshot")
        self.harness.frame(ModelSnapshotBeginCommand(self.fresh_snapshot_id, 1, 2, 1, 1, 0), "begin_snapshot", key=self.fresh_stream)
        self.fresh_snapshot_stage = 1

    @precondition(lambda self: self.fresh_snapshot_id is not None and self.fresh_snapshot_stage == 1)
    @rule()
    def fresh_stream_snapshot_chat_chunk(self) -> None:
        assert self.fresh_snapshot_id is not None
        self.harness.frame(ModelSnapshotChunkCommand(self.fresh_snapshot_id, 0, "chat", [chat_record("base")]), "add_snapshot_chunk", key=self.fresh_stream)
        self.fresh_snapshot_stage = 2

    @precondition(lambda self: self.fresh_snapshot_id is not None and self.fresh_snapshot_stage == 2)
    @rule()
    def fresh_stream_snapshot_message_chunk(self) -> None:
        assert self.fresh_snapshot_id is not None
        self.harness.frame(ModelSnapshotChunkCommand(self.fresh_snapshot_id, 1, "message", [message_record(self.message_id, "base")]), "add_snapshot_chunk", key=self.fresh_stream)
        self.fresh_snapshot_stage = 3

    @precondition(lambda self: self.fresh_snapshot_id is not None and self.fresh_snapshot_stage == 3)
    @rule()
    def fresh_stream_snapshot_commit(self) -> None:
        assert self.fresh_snapshot_id is not None
        self.harness.frame(ModelSnapshotCommitCommand(self.fresh_snapshot_id, 2), "commit_snapshot", "N20", key=self.fresh_stream)
        self.fresh_snapshot_stage = 4

    @precondition(lambda self: self.fresh_snapshot_id is not None and self.fresh_snapshot_stage == 4)
    @rule()
    def replay_committed_fresh_snapshot_lifecycle(self) -> None:
        """Lost-ACK retry of every already committed fresh-stream frame."""
        assert self.fresh_snapshot_id is not None
        self.harness.frame(ModelSnapshotBeginCommand(self.fresh_snapshot_id, 1, 2, 1, 1, 0), "begin_snapshot", "N02", key=self.fresh_stream)
        self.harness.frame(ModelSnapshotChunkCommand(self.fresh_snapshot_id, 0, "chat", [chat_record("base")]), "add_snapshot_chunk", "N07", key=self.fresh_stream)
        self.harness.frame(ModelSnapshotChunkCommand(self.fresh_snapshot_id, 1, "message", [message_record(self.message_id, "base")]), "add_snapshot_chunk", "N07", key=self.fresh_stream)
        self.harness.frame(ModelSnapshotCommitCommand(self.fresh_snapshot_id, 2), "commit_snapshot", "N15", key=self.fresh_stream)

    @rule()
    def reconstruct_repository(self) -> None:
        self.harness.reconstruct_repository()


@pytest.mark.stateful_tier_a
class TestBrainIngestionGeneral(BrainIngestionStateMachine.TestCase):
    pass


@pytest.mark.stateful_tier_a
class TestBrainIngestionDeletion(BrainIngestionDeletionStateMachine.TestCase):
    pass


def test_catalogue_is_exact_and_all_scenarios_execute() -> None:
    assert set().union(*CATALOGUE_SCENARIOS.values()) == CATALOGUE_IDS
    assert len(CATALOGUE_IDS) == 42
    assert all(callable(getattr(Harness, name, None)) for name in CATALOGUE_SCENARIOS)
    harness = Harness("catalogue")
    harness.exercise_deltas(); harness.exercise_anomalies(); harness.exercise_snapshots()
    assert set().union(*(set(entry["requirements"]) for entry in harness.trace)) == CATALOGUE_IDS
    assert all(set(entry) == {"operation", "stream_key", "command", "requirements"} for entry in harness.trace)
    assert all(set(entry["stream_key"]) == {"creator_account_id", "agent_installation_id", "agent_stream_id"} for entry in harness.trace)
    assert any(entry["operation"] == "configure_coverage" for entry in harness.trace)
    assert any(entry["operation"] == "reconstruct_repository" for entry in harness.trace)
    json.dumps(harness.trace, sort_keys=True)


def test_generated_rules_cover_separate_snapshot_and_coverage_transitions() -> None:
    """Keep generated-rule families explicit; do not rely on catalogue-only cases."""
    machine = BrainIngestionStateMachine()
    machine.start()
    machine.old_sequence_replay()
    machine.establish_immutable_message(1)
    machine.immutable_message_conflict()
    machine.coverage_generation_started()
    machine.coverage_inventory_member()
    machine.coverage_inventory_ended()
    machine.coverage_history_started()
    machine.coverage_head_reconciled()
    machine.coverage_generation_closed()
    machine.begin_staged_snapshot()
    machine.duplicate_staged_snapshot_begin()
    machine.conflicting_staged_snapshot_begin()
    machine.skipped_staged_snapshot_chunk()
    machine.staged_snapshot_chat_chunk(2)
    machine.duplicate_staged_snapshot_chunk()
    machine.conflicting_staged_snapshot_chunk()
    machine.reconstruct_while_snapshot_staged()
    machine.staged_snapshot_message_chunk()
    machine.commit_staged_snapshot()
    machine.begin_ordering_snapshot()
    machine.ordering_message_first()
    machine.ordering_chat_after_message()
    machine.generated_snapshot_chat_conflicts(3)
    machine.generated_snapshot_orphan_message(4)
    machine.generated_snapshot_chat_tombstone_cascade(5)
    assert {"A04", "A07", "D09", "N02", "N03", "N07", "N08", "N09", "N10"} <= set().union(
        *(set(entry["requirements"]) for entry in machine.harness.trace)
    )
    assert any(entry["operation"] == "reconstruct_repository" for entry in machine.harness.trace)
    json.dumps(machine.harness.trace, sort_keys=True)


def test_generated_deletion_rules_use_fresh_stream_then_replay_committed_lifecycle() -> None:
    machine = BrainIngestionDeletionStateMachine()
    machine.start()
    machine.fresh_stream_snapshot_begin()
    machine.fresh_stream_snapshot_chat_chunk()
    machine.fresh_stream_snapshot_message_chunk()
    machine.fresh_stream_snapshot_commit()
    machine.replay_committed_fresh_snapshot_lifecycle()
    observed = machine.harness.adapter.observe_state(machine.fresh_stream)
    assert ("chat", "base") in observed.tombstones
    assert ("message", machine.message_id) in observed.tombstones
    replay = [entry for entry in machine.harness.trace if entry["stream_key"] == _trace_key(machine.fresh_stream)]
    assert [entry["operation"] for entry in replay][-4:] == ["begin_snapshot", "add_snapshot_chunk", "add_snapshot_chunk", "commit_snapshot"]


def test_repository_owned_profile_calibrations() -> None:
    assert settings.get_profile("tier_a_general").max_examples == 15
    assert settings.get_profile("tier_a_general").stateful_step_count == 20
    assert settings.get_profile("tier_a_deletion").max_examples == 10
    assert settings.get_profile("tier_a_deletion").stateful_step_count == 20
    assert settings.get_profile("dev").max_examples == 8
    assert settings.get_profile("dev").stateful_step_count == 12


def test_rejected_anomalies_recover_at_checkpoint_plus_one() -> None:
    harness = Harness("liveness"); harness.exercise_anomalies()
    assert harness.model.checkpoint(harness.key) == harness.adapter.observe_state(harness.key).checkpoint


def _normalized_state(harness: Harness) -> tuple[Any, ...]:
    observed = harness.adapter.observe_state(harness.key)
    return (observed.checkpoint, observed.canonical_revision, observed.active_chats,
            observed.active_messages, observed.tombstones, observed.pending_snapshot,
            observed.staged_record_counts, observed.coverage)


def test_metamorphic_retry_and_in_memory_reconstruction() -> None:
    """Clean == lost-ACK retry == repository reconstruction; disk reopen is Task 5C."""
    clean = Harness("metamorphic"); retried = Harness("metamorphic"); reconstructed = Harness("metamorphic")
    command = clean.chat("metamorphic-chat")
    clean.frame(command, "commit_delta", "D01")
    retried.frame(command, "commit_delta", "D01"); retried.frame(command, "commit_delta", "A01")
    reconstructed.frame(command, "commit_delta", "D01"); reconstructed.reconstruct_repository()
    assert _normalized_state(clean) == _normalized_state(retried)
    assert _normalized_state(clean) == _normalized_state(reconstructed)


def test_snapshot_older_chat_does_not_replace_newer_canonical_chat() -> None:
    harness = Harness("snapshot-older-chat")
    harness.frame(harness.chat("base", name="newer", updated_at="2026-07-19T13:00:00+00:00"), "commit_delta", "D04")
    stream, snapshot = harness.stream("older"), harness.new_id("older")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [chat_record("base", name="older")]), "add_snapshot_chunk", key=stream)
    harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert harness.adapter.observe_state(stream).active_chats["base"]["display_name"] == "newer"


def test_snapshot_changed_existing_message_is_rejected_without_mutation() -> None:
    harness = Harness("snapshot-message-conflict")
    harness.frame(harness.message("immutable", "base", text="first"), "commit_delta", "D06")
    stream, snapshot = harness.stream("message-conflict"), harness.new_id("message-conflict")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 0, 1, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "message", [message_record("immutable", "base")]), "add_snapshot_chunk", key=stream)
    outcome = harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert outcome.disposition == "rejected"
    assert harness.adapter.observe_state(stream).active_messages["immutable"]["text"] == "first"


def test_inventory_member_after_generation_started_is_observed_exactly() -> None:
    harness = Harness("coverage-member")
    harness.configure_coverage(); generation = harness.new_id("generation")
    harness.frame(ModelCoverageObservedCommand(harness.new_id("event"), harness.next_seq(), "generation.started", {"generation_id": generation, "as_of": "2026-07-19T12:00:00Z", "authorization_revision": "consent-1"}), "commit_delta", "D09")
    harness.frame(ModelCoverageObservedCommand(harness.new_id("event"), harness.next_seq(), "inventory.member", {"generation_id": generation, "conversation_id": "base"}), "commit_delta", "D09")
    coverage = harness.adapter.observe_state(harness.key).coverage
    assert coverage["generation_id"] == str(generation) and coverage["discovered_conversations"] == 1


def test_snapshot_chat_tombstone_cascades_existing_live_messages() -> None:
    harness = Harness("snapshot-chat-cascade")
    harness.frame(harness.message("cascade-child", "base"), "commit_delta", "D06")
    stream, snapshot = harness.stream("cascade"), harness.new_id("cascade")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [{"tombstone": True, "chat_id": "base"}]), "add_snapshot_chunk", key=stream)
    harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    observed = harness.adapter.observe_state(stream)
    assert "base" not in observed.active_chats and "cascade-child" not in observed.active_messages
    assert ("chat", "base") in observed.tombstones


def test_snapshot_message_tombstone_rejects_wrong_parent() -> None:
    harness = Harness("snapshot-tombstone-parent")
    harness.frame(harness.message("parent-bound", "base"), "commit_delta", "D06")
    stream, snapshot = harness.stream("wrong-parent"), harness.new_id("wrong-parent")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 0, 1, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "message", [{"tombstone": True, "message_id": "parent-bound", "chat_id": "wrong-parent"}]), "add_snapshot_chunk", key=stream)
    outcome = harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert outcome.disposition == "rejected"
    assert "parent-bound" in harness.adapter.observe_state(stream).active_messages


def test_snapshot_live_message_rejects_absent_parent() -> None:
    harness = Harness("snapshot-orphan")
    stream, snapshot = harness.stream("orphan"), harness.new_id("orphan")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 0, 1, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "message", [message_record("snapshot-orphan", "absent-parent")]), "add_snapshot_chunk", key=stream)
    outcome = harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert outcome.disposition == "rejected"


def test_snapshot_chat_rejects_conflicting_platform_identity() -> None:
    harness = Harness("snapshot-platform-conflict")
    stream, snapshot = harness.stream("platform"), harness.new_id("platform")
    record = chat_record("base")
    record["chat"]["platform_user_id"] = "different-fan"
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [record]), "add_snapshot_chunk", key=stream)
    outcome = harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert outcome.disposition == "rejected"


def test_snapshot_chat_rejects_equal_timestamp_conflicting_content() -> None:
    harness = Harness("snapshot-chat-version-conflict")
    stream, snapshot = harness.stream("version"), harness.new_id("version")
    record = chat_record("base", name="different-at-same-version")
    harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=stream)
    harness.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [record]), "add_snapshot_chunk", key=stream)
    outcome = harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=stream)
    assert outcome.disposition == "rejected"


def test_snapshot_rejects_duplicate_staged_identifiers_within_and_across_chunks() -> None:
    harness = Harness("snapshot-duplicate-identifiers")
    within_stream, within_snapshot = harness.stream("within"), harness.new_id("within")
    duplicate = chat_record("duplicate-within")
    harness.frame(ModelSnapshotBeginCommand(within_snapshot, 1, 1, 2, 0, 0), "begin_snapshot", key=within_stream)
    within = harness.frame(ModelSnapshotChunkCommand(within_snapshot, 0, "chat", [duplicate, duplicate]), "add_snapshot_chunk", key=within_stream)
    assert within.disposition == "rejected"

    across_stream, across_snapshot = harness.stream("across"), harness.new_id("across")
    harness.frame(ModelSnapshotBeginCommand(across_snapshot, 1, 2, 2, 0, 0), "begin_snapshot", key=across_stream)
    harness.frame(ModelSnapshotChunkCommand(across_snapshot, 0, "chat", [chat_record("duplicate-across")]), "add_snapshot_chunk", key=across_stream)
    across = harness.frame(ModelSnapshotChunkCommand(across_snapshot, 1, "chat", [chat_record("duplicate-across")]), "add_snapshot_chunk", key=across_stream)
    assert across.disposition == "rejected"
