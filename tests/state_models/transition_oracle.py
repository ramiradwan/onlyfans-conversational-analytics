"""Compare each production ingestion transition with the pure model."""

from __future__ import annotations

import json
import pprint
from typing import Any

from tests.state_models.brain_ingestion_model import (
    ModelStreamKey,
    ModelTransitionOutcome,
    PureBrainIngestionModel,
)
from tests.state_models.production_brain_adapter import (
    ProductionBrainAdapter,
    ProductionStateSnapshot,
    ProductionTransitionOutcome,
)


class OracleMismatchError(AssertionError):
    """Raised when pure model and production state disagree."""

    def __init__(
        self,
        message: str,
        *,
        command: Any,
        model_outcome: ModelTransitionOutcome,
        prod_outcome: ProductionTransitionOutcome,
        command_history: list[Any],
        snapshot_before: ProductionStateSnapshot | None = None,
        snapshot_after: ProductionStateSnapshot | None = None,
    ) -> None:
        formatted_history = "\n".join(
            f"  [{idx:03d}] {json.dumps(step, sort_keys=True, default=str)}"
            for idx, step in enumerate(command_history)
        )
        full_message = (
            f"\n{'='*75}\n"
            f"ORACLE INVARIANT MISMATCH: {message}\n"
            f"{'='*75}\n"
            f"Failing Transition: {type(command).__name__}\n"
            f"Command: {command}\n\n"
            f"Model Outcome:      {model_outcome}\n"
            f"Production Outcome: {prod_outcome}\n\n"
            f"Snapshot Before:\n{pprint.pformat(snapshot_before, indent=2)}\n\n"
            f"Snapshot After:\n{pprint.pformat(snapshot_after, indent=2)}\n\n"
            f"Structured Replayable Command Trace ({len(command_history)} steps):\n"
            f"{formatted_history}\n"
            f"{'='*75}\n"
        )
        super().__init__(full_message)


def assert_transition_oracle(
    *,
    model: PureBrainIngestionModel,
    adapter: ProductionBrainAdapter,
    key: ModelStreamKey,
    command: Any,
    model_outcome: ModelTransitionOutcome,
    prod_outcome: ProductionTransitionOutcome,
    snapshot_before: ProductionStateSnapshot,
    snapshot_after: ProductionStateSnapshot,
    command_history: list[Any],
) -> None:
    """Evaluate all required oracle invariants after a state transition."""

    def fail(diagnostic: str) -> None:
        raise OracleMismatchError(
            diagnostic,
            command=command,
            model_outcome=model_outcome,
            prod_outcome=prod_outcome,
            command_history=command_history,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
        )

    # 1. Disposition equality
    if prod_outcome.disposition != model_outcome.disposition:
        fail(
            f"Disposition disagreement: model={model_outcome.disposition!r} "
            f"vs production={prod_outcome.disposition!r}"
        )

    # 2. Every field exposed by the public outcome is contractual.
    for field in (
        "code", "retryable", "committed_source_seq", "snapshot_id",
        "next_expected_chunk_index", "snapshot_committed", "canonical_revision",
    ):
        if getattr(prod_outcome, field) != getattr(model_outcome, field):
            fail(
                f"Outcome {field} disagreement: model={getattr(model_outcome, field)!r} "
                f"vs production={getattr(prod_outcome, field)!r}"
            )

    # 6. Stream checkpoint equality
    expected_cp = model.checkpoint(key)
    if snapshot_after.checkpoint != expected_cp:
        fail(
            f"Stream checkpoint disagreement: model={expected_cp} "
            f"vs production={snapshot_after.checkpoint}"
        )

    # 7. Canonical revision equality
    if snapshot_after.canonical_revision != model.canonical_revision:
        fail(
            f"Canonical revision disagreement: model={model.canonical_revision} "
            f"vs production={snapshot_after.canonical_revision}"
        )

    # 8. Active chats matching
    model_chats = model.active_chats()
    prod_chats = snapshot_after.active_chats
    if set(model_chats.keys()) != set(prod_chats.keys()):
        fail(
            f"Active chats set disagreement: model={sorted(model_chats.keys())} "
            f"vs production={sorted(prod_chats.keys())}"
        )
    for chat_id, m_chat in model_chats.items():
        p_chat = prod_chats[chat_id]
        if m_chat.record_kind != p_chat["record_kind"]:
            fail(f"Chat {chat_id} record_kind disagreement: model={m_chat.record_kind!r} vs prod={p_chat['record_kind']!r}")
        if m_chat.platform_user_id != p_chat["platform_user_id"]:
            fail(f"Chat {chat_id} platform_user_id disagreement: model={m_chat.platform_user_id!r} vs prod={p_chat['platform_user_id']!r}")
        if m_chat.display_name != p_chat["display_name"]:
            fail(f"Chat {chat_id} display_name disagreement: model={m_chat.display_name!r} vs prod={p_chat['display_name']!r}")
        if m_chat.updated_at != p_chat["updated_at"]:
            fail(f"Chat {chat_id} updated_at disagreement: model={m_chat.updated_at!r} vs prod={p_chat['updated_at']!r}")

    # 9. Active messages matching
    model_msgs = model.active_messages()
    prod_msgs = snapshot_after.active_messages
    if set(model_msgs.keys()) != set(prod_msgs.keys()):
        fail(
            f"Active messages set disagreement: model={sorted(model_msgs.keys())} "
            f"vs production={sorted(prod_msgs.keys())}"
        )
    for msg_id, m_msg in model_msgs.items():
        p_msg = prod_msgs[msg_id]
        if m_msg.chat_id != p_msg["chat_id"]:
            fail(f"Message {msg_id} chat_id disagreement: model={m_msg.chat_id!r} vs prod={p_msg['chat_id']!r}")
        if m_msg.text != p_msg["text"]:
            fail(f"Message {msg_id} text disagreement: model={m_msg.text!r} vs prod={p_msg['text']!r}")
        if m_msg.direction != p_msg["direction"]:
            fail(f"Message {msg_id} direction disagreement: model={m_msg.direction!r} vs prod={p_msg['direction']!r}")
        if m_msg.sender_platform_user_id != p_msg["sender_platform_user_id"]:
            fail(f"Message {msg_id} sender disagreement: model={m_msg.sender_platform_user_id!r} vs prod={p_msg['sender_platform_user_id']!r}")
        if m_msg.sent_at != p_msg["sent_at"]:
            fail(f"Message {msg_id} sent_at disagreement: model={m_msg.sent_at!r} vs prod={p_msg['sent_at']!r}")

    # 10. Tombstones matching
    if snapshot_after.tombstones != model.tombstones():
        fail(
            f"Tombstones disagreement: model={sorted(model.tombstones())} "
            f"vs production={sorted(snapshot_after.tombstones)}"
        )

    # 11. Pending snapshot matching
    expected_pending = model.pending_snapshot(key)
    if snapshot_after.pending_snapshot != expected_pending:
        fail(
            f"Pending snapshot disagreement: model={expected_pending} "
            f"vs production={snapshot_after.pending_snapshot}"
        )

    # 12. Exact staging and D09 public coverage state.
    if snapshot_after.staged_record_counts != model.staged_record_counts(key):
        fail(f"Staged record counts disagreement: model={model.staged_record_counts(key)} vs production={snapshot_after.staged_record_counts}")
    if snapshot_after.coverage != model.coverage:
        fail(f"Coverage state disagreement: model={model.coverage} vs production={snapshot_after.coverage}")

    # 13. Staging isolation
    if snapshot_after.pending_snapshot is not None:
        # Assert uncommitted staged records are not visible in canonical queries
        staged_chats_count = snapshot_after.staged_record_counts["chats"]
        staged_msgs_count = snapshot_after.staged_record_counts["messages"]
        if staged_chats_count > 0 or staged_msgs_count > 0:
            # Active chats count must equal committed model chats count
            if len(prod_chats) != len(model_chats):
                fail("Staging isolation leak: staged chat records visible in canonical active chats")
            if len(prod_msgs) != len(model_msgs):
                fail("Staging isolation leak: staged message records visible in canonical active messages")

    # 14. Non-mutation on duplicate, gap, and rejected
    if prod_outcome.disposition in ("duplicate", "gap", "rejected"):
        if snapshot_after.checkpoint != snapshot_before.checkpoint:
            fail(
                f"Partial mutation on {prod_outcome.disposition}: checkpoint changed from "
                f"{snapshot_before.checkpoint} to {snapshot_after.checkpoint}"
            )
        if snapshot_after.canonical_revision != snapshot_before.canonical_revision:
            fail(
                f"Partial mutation on {prod_outcome.disposition}: canonical revision changed from "
                f"{snapshot_before.canonical_revision} to {snapshot_after.canonical_revision}"
            )
        if snapshot_after.active_chats != snapshot_before.active_chats:
            fail(f"Partial mutation on {prod_outcome.disposition}: active chats mutated")
        if snapshot_after.active_messages != snapshot_before.active_messages:
            fail(f"Partial mutation on {prod_outcome.disposition}: active messages mutated")
        if snapshot_after.tombstones != snapshot_before.tombstones:
            fail(f"Partial mutation on {prod_outcome.disposition}: tombstones mutated")
        if snapshot_after.pending_snapshot != snapshot_before.pending_snapshot:
            fail(f"Partial mutation on {prod_outcome.disposition}: pending snapshot mutated")
        if snapshot_after.staged_record_counts != snapshot_before.staged_record_counts:
            fail(f"Partial mutation on {prod_outcome.disposition}: staging mutated")
        if snapshot_after.coverage != snapshot_before.coverage:
            fail(f"Partial mutation on {prod_outcome.disposition}: coverage mutated")
