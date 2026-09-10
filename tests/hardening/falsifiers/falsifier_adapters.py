"""Deliberately broken adapters acting as permanent negative controls.

Used by test_falsifiers.py to prove the Task 5 transition oracle actively
detects invariant violations across:
1. BrokenGapAdapter: advances checkpoint on a gap.
2. BrokenDuplicateAdapter: mutates revision/state on exact duplicate.
3. BrokenDeletionAdapter: permits tombstoned material to return.
4. BrokenReopenAdapter: simulates committed-state loss before reconstruction.
"""

from __future__ import annotations

from typing import Any

from tests.state_models.brain_ingestion_model import (
    ModelChatUpsertCommand,
    ModelMessageUpsertCommand,
    ModelStreamKey,
)
from tests.state_models.production_brain_adapter import (
    ProductionBrainAdapter,
    ProductionTransitionOutcome,
)


class BrokenGapAdapter(ProductionBrainAdapter):
    """Broken test double that illegally advances the checkpoint on a sequence gap."""

    def commit_delta(
        self,
        key: ModelStreamKey,
        cmd: Any,
    ) -> ProductionTransitionOutcome:
        res = super().commit_delta(key, cmd)
        if res.disposition == "gap":
            # Illegally advance persistent checkpoint to the gap sequence
            with self.database.transaction() as conn:
                conn.execute(
                    """UPDATE ingest_checkpoints SET committed_source_seq = ?
                       WHERE creator_account_id = ? AND agent_installation_id = ? AND agent_stream_id = ?""",
                    (
                        cmd.source_seq,
                        key.creator_account_id,
                        str(key.agent_installation_id),
                        str(key.agent_stream_id),
                    ),
                )
        return res


class BrokenDuplicateAdapter(ProductionBrainAdapter):
    """Broken test double that illegally mutates canonical revision on exact duplicate."""

    def commit_delta(
        self,
        key: ModelStreamKey,
        cmd: Any,
    ) -> ProductionTransitionOutcome:
        res = super().commit_delta(key, cmd)
        if res.disposition == "duplicate":
            # Illegally increment canonical revision in persistent database
            with self.database.transaction() as conn:
                conn.execute(
                    """UPDATE account_heads SET canonical_revision = canonical_revision + 1
                       WHERE creator_account_id = ?""",
                    (key.creator_account_id,),
                )
        return res


class BrokenDeletionAdapter(ProductionBrainAdapter):
    """Broken test double that allows tombstoned entities to be resurrected."""

    def commit_delta(
        self,
        key: ModelStreamKey,
        cmd: Any,
    ) -> ProductionTransitionOutcome:
        res = super().commit_delta(key, cmd)
        # Illegally resurrect tombstoned material when an upsert arrives
        if isinstance(cmd, ModelChatUpsertCommand):
            with self.database.transaction() as conn:
                conn.execute(
                    """UPDATE account_chats SET is_deleted = 0
                       WHERE creator_account_id = ? AND chat_id = ?""",
                    (key.creator_account_id, cmd.chat_id),
                )
                conn.execute(
                    """DELETE FROM entity_tombstones
                       WHERE creator_account_id = ? AND entity_kind = 'chat' AND entity_id = ?""",
                    (key.creator_account_id, cmd.chat_id),
                )
        elif isinstance(cmd, ModelMessageUpsertCommand):
            with self.database.transaction() as conn:
                conn.execute(
                    """UPDATE account_messages SET is_deleted = 0
                       WHERE creator_account_id = ? AND message_id = ?""",
                    (key.creator_account_id, cmd.message_id),
                )
                conn.execute(
                    """DELETE FROM entity_tombstones
                       WHERE creator_account_id = ? AND entity_kind = 'message' AND entity_id = ?""",
                    (key.creator_account_id, cmd.message_id),
                )
        return res


class BrokenReopenAdapter(ProductionBrainAdapter):
    """Broken control that corrupts committed state before dependency reconstruction."""

    def drop_committed_state_before_reconstruction(self, key: ModelStreamKey) -> None:
        """Deliberately delete a checkpoint so the reconstruction oracle must fail."""
        with self.database.transaction() as conn:
            conn.execute(
                """DELETE FROM ingest_checkpoints
                   WHERE creator_account_id = ? AND agent_installation_id = ? AND agent_stream_id = ?""",
                (
                    key.creator_account_id,
                    str(key.agent_installation_id),
                    str(key.agent_stream_id),
                ),
            )


class BrokenStagedMaterialAdapter(ProductionBrainAdapter):
    """Broken control that changes a staged chunk after it was acknowledged."""

    def corrupt_staged_material(self, key: ModelStreamKey, snapshot_id: Any) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """UPDATE snapshot_chunks SET fingerprint='corrupted-staged-material'
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                (key.creator_account_id, str(key.agent_installation_id), str(key.agent_stream_id), str(snapshot_id)),
            )
