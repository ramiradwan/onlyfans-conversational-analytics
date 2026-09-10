"""Executable verification proving the oracle rejects Task 5 falsifiers.

Falsifiers tested:
1. BrokenGapAdapter: advances checkpoint on a gap -> catches checkpoint-monotonicity violation.
2. BrokenDuplicateAdapter: mutates revision on duplicate -> catches replay-idempotency violation.
3. BrokenDeletionAdapter: resurrects tombstoned entity -> catches deletion-closure violation.
4. BrokenReopenAdapter: corrupts state before reconstruction -> catches state-loss divergence.
5. BrokenStagedMaterialAdapter: corrupts acknowledged staging -> catches staging divergence.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.persistence.factory import create_canonical_repositories
from tests.hardening.falsifiers.falsifier_adapters import (
    BrokenDeletionAdapter,
    BrokenDuplicateAdapter,
    BrokenGapAdapter,
    BrokenReopenAdapter,
    BrokenStagedMaterialAdapter,
)
from tests.state_models.brain_ingestion_model import (
    ModelChatDeleteCommand,
    ModelChatUpsertCommand,
    ModelSnapshotBeginCommand,
    ModelSnapshotChunkCommand,
    ModelSnapshotCommitCommand,
    ModelStreamKey,
    PureBrainIngestionModel,
)
from tests.state_models.transition_oracle import (
    OracleMismatchError,
    assert_transition_oracle,
)


ACCOUNT_ID = "falsifier-test-account"
INSTALLATION_ID = UUID("40000000-0000-4000-8000-000000000001")
STREAM_ID = UUID("50000000-0000-4000-8000-000000000001")


def _setup_environment(adapter_cls=None):
    repos = create_canonical_repositories("memory")
    history = repos.history
    db = repos.database
    key = ModelStreamKey(ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
    model = PureBrainIngestionModel(ACCOUNT_ID)

    if adapter_cls is None:
        from tests.state_models.production_brain_adapter import ProductionBrainAdapter
        adapter = ProductionBrainAdapter(history, db)
    else:
        adapter = adapter_cls(history, db)

    # Establish baseline snapshot at through_seq=1
    snap_id = uuid4()
    begin_cmd = ModelSnapshotBeginCommand(
        snapshot_id=snap_id,
        through_seq=1,
        chunk_count=1,
        expected_chats=1,
        expected_messages=0,
        expected_coverage=0,
    )
    m_out = model.begin_snapshot(key, begin_cmd)
    p_out = adapter.begin_snapshot(key, begin_cmd)

    chunk_cmd = ModelSnapshotChunkCommand(
        snapshot_id=snap_id,
        chunk_index=0,
        entity_kind="chat",
        records=[{
            "tombstone": False,
            "chat": {
                "record_kind": "full",
                "chat_id": "chat-base",
                "platform_user_id": "fan-base",
                "display_name": "Base Chat",
                "updated_at": "2026-07-19T10:00:00Z",
            },
        }],
    )
    model.add_snapshot_chunk(key, chunk_cmd)
    adapter.add_snapshot_chunk(key, chunk_cmd)

    commit_cmd = ModelSnapshotCommitCommand(snapshot_id=snap_id, chunk_count=1)
    model.commit_snapshot(key, commit_cmd)
    adapter.commit_snapshot(key, commit_cmd)

    history_log = [begin_cmd, chunk_cmd, commit_cmd]
    return repos, model, adapter, key, history_log


def test_broken_gap_adapter() -> None:
    """BrokenGapAdapter must fail the oracle with a checkpoint advancement diagnostic."""
    repos, model, adapter, key, history_log = _setup_environment(BrokenGapAdapter)

    # Issue a forward gap command (seq=5 when checkpoint=1)
    gap_cmd = ModelChatUpsertCommand(
        event_id=uuid4(),
        source_seq=5,
        chat_id="chat-gap",
        record_kind="full",
        platform_user_id="fan-gap",
        display_name="Gap Chat",
        updated_at="2026-07-19T10:05:00Z",
    )

    snap_before = adapter.observe_state(key)
    m_out = model.commit_delta(key, gap_cmd)
    p_out = adapter.commit_delta(key, gap_cmd)
    snap_after = adapter.observe_state(key)

    with pytest.raises(OracleMismatchError) as exc_info:
        assert_transition_oracle(
            model=model,
            adapter=adapter,
            key=key,
            command=gap_cmd,
            model_outcome=m_out,
            prod_outcome=p_out,
            snapshot_before=snap_before,
            snapshot_after=snap_after,
            command_history=history_log + [gap_cmd],
        )

    diag = str(exc_info.value)
    assert "checkpoint" in diag.lower(), f"Diagnostic must cite checkpoint violation, got: {diag}"
    assert "checkpoint-monotonicity" in diag.lower() or "partial mutation on gap" in diag.lower() or "disagreement" in diag.lower()


def test_broken_duplicate_adapter() -> None:
    """BrokenDuplicateAdapter must fail the oracle with a revision mutation diagnostic."""
    repos, model, adapter, key, history_log = _setup_environment(BrokenDuplicateAdapter)

    # Commit valid delta at sequence 2
    delta_cmd = ModelChatUpsertCommand(
        event_id=uuid4(),
        source_seq=2,
        chat_id="chat-2",
        record_kind="full",
        platform_user_id="fan-2",
        display_name="Chat Two",
        updated_at="2026-07-19T10:02:00Z",
    )
    snap_before = adapter.observe_state(key)
    m_out = model.commit_delta(key, delta_cmd)
    p_out = adapter.commit_delta(key, delta_cmd)
    snap_after = adapter.observe_state(key)
    assert_transition_oracle(
        model=model,
        adapter=adapter,
        key=key,
        command=delta_cmd,
        model_outcome=m_out,
        prod_outcome=p_out,
        snapshot_before=snap_before,
        snapshot_after=snap_after,
        command_history=history_log + [delta_cmd],
    )
    history_log.append(delta_cmd)

    # Re-transmit exact duplicate delta
    snap_before_dup = adapter.observe_state(key)
    m_out_dup = model.commit_delta(key, delta_cmd)
    p_out_dup = adapter.commit_delta(key, delta_cmd)
    snap_after_dup = adapter.observe_state(key)

    with pytest.raises(OracleMismatchError) as exc_info:
        assert_transition_oracle(
            model=model,
            adapter=adapter,
            key=key,
            command=delta_cmd,
            model_outcome=m_out_dup,
            prod_outcome=p_out_dup,
            snapshot_before=snap_before_dup,
            snapshot_after=snap_after_dup,
            command_history=history_log + [delta_cmd],
        )

    diag = str(exc_info.value)
    assert "canonical revision" in diag.lower(), f"Diagnostic must cite revision mutation, got: {diag}"


def test_broken_deletion_adapter() -> None:
    """BrokenDeletionAdapter must fail the oracle with a tombstone resurrection diagnostic."""
    repos, model, adapter, key, history_log = _setup_environment(BrokenDeletionAdapter)

    # Delete base chat at sequence 2
    del_cmd = ModelChatDeleteCommand(
        event_id=uuid4(),
        source_seq=2,
        chat_id="chat-base",
    )
    snap_before_del = adapter.observe_state(key)
    m_out_del = model.commit_delta(key, del_cmd)
    p_out_del = adapter.commit_delta(key, del_cmd)
    snap_after_del = adapter.observe_state(key)
    assert_transition_oracle(
        model=model,
        adapter=adapter,
        key=key,
        command=del_cmd,
        model_outcome=m_out_del,
        prod_outcome=p_out_del,
        snapshot_before=snap_before_del,
        snapshot_after=snap_after_del,
        command_history=history_log + [del_cmd],
    )
    history_log.append(del_cmd)

    # Stale upsert attempting to resurrect tombstoned chat at sequence 3
    resurrect_cmd = ModelChatUpsertCommand(
        event_id=uuid4(),
        source_seq=3,
        chat_id="chat-base",
        record_kind="full",
        platform_user_id="fan-base",
        display_name="Revived Chat",
        updated_at="2026-07-19T10:10:00Z",
    )
    snap_before_res = adapter.observe_state(key)
    m_out_res = model.commit_delta(key, resurrect_cmd)
    p_out_res = adapter.commit_delta(key, resurrect_cmd)
    snap_after_res = adapter.observe_state(key)

    with pytest.raises(OracleMismatchError) as exc_info:
        assert_transition_oracle(
            model=model,
            adapter=adapter,
            key=key,
            command=resurrect_cmd,
            model_outcome=m_out_res,
            prod_outcome=p_out_res,
            snapshot_before=snap_before_res,
            snapshot_after=snap_after_res,
            command_history=history_log + [resurrect_cmd],
        )

    diag = str(exc_info.value)
    assert "tombstone" in diag.lower() or "active chats" in diag.lower(), (
        f"Diagnostic must cite tombstone or active chats resurrection violation, got: {diag}"
    )


def test_broken_reopen_adapter() -> None:
    """State corruption before dependency reconstruction must fail the oracle."""
    repos, model, adapter, key, history_log = _setup_environment(BrokenReopenAdapter)

    # State before simulated drop
    snap_before = adapter.observe_state(key)
    assert snap_before.checkpoint == 1

    # This is intentional corruption, not Task 5C file-backed close/reopen evidence.
    adapter.drop_committed_state_before_reconstruction(key)
    snap_after = adapter.observe_state(key)

    # Create dummy check transition
    dummy_cmd = history_log[-1]
    last_m_out = model.begin_snapshot(key, history_log[0])  # duplicate outcome
    last_p_out = adapter.begin_snapshot(key, history_log[0])

    with pytest.raises(OracleMismatchError) as exc_info:
        assert_transition_oracle(
            model=model,
            adapter=adapter,
            key=key,
            command=dummy_cmd,
            model_outcome=last_m_out,
            prod_outcome=last_p_out,
            snapshot_before=snap_before,
            snapshot_after=snap_after,
            command_history=history_log,
        )

    diag = str(exc_info.value)
    assert "checkpoint" in diag.lower() or "disagreement" in diag.lower(), (
        f"Diagnostic must cite checkpoint loss across reconstruction, got: {diag}"
    )


def test_broken_staged_material_adapter() -> None:
    """A duplicate chunk exposes staged-material corruption through the oracle."""
    repos = create_canonical_repositories("memory")
    key = ModelStreamKey(ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
    model = PureBrainIngestionModel(ACCOUNT_ID)
    adapter = BrokenStagedMaterialAdapter(repos.history, repos.database)
    snapshot_id = UUID("60000000-0000-4000-8000-000000000001")
    begin = ModelSnapshotBeginCommand(snapshot_id, 1, 1, 1, 0, 0)
    chunk = ModelSnapshotChunkCommand(snapshot_id, 0, "chat", [{"tombstone": False, "chat": {"record_kind": "full", "chat_id": "stage", "platform_user_id": "fan", "display_name": "stage", "updated_at": "2026-07-19T10:00:00Z"}}])
    for command, operation in ((begin, "begin_snapshot"), (chunk, "add_snapshot_chunk"), (chunk, "add_snapshot_chunk")):
        before = adapter.observe_state(key); model_outcome = getattr(model, operation)(key, command); prod_outcome = getattr(adapter, operation)(key, command); after = adapter.observe_state(key)
        assert_transition_oracle(model=model, adapter=adapter, key=key, command=command, model_outcome=model_outcome, prod_outcome=prod_outcome, snapshot_before=before, snapshot_after=after, command_history=[begin, chunk])
    adapter.corrupt_staged_material(key, snapshot_id)
    before = adapter.observe_state(key); model_outcome = model.add_snapshot_chunk(key, chunk); prod_outcome = adapter.add_snapshot_chunk(key, chunk); after = adapter.observe_state(key)
    with pytest.raises(OracleMismatchError, match="Outcome|Pending snapshot|staging"):
        assert_transition_oracle(model=model, adapter=adapter, key=key, command=chunk, model_outcome=model_outcome, prod_outcome=prod_outcome, snapshot_before=before, snapshot_after=after, command_history=[begin, chunk, chunk])
