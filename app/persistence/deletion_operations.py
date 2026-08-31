"""Durable truthfulness for whole-Vault deletion completion."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.persistence.database import CanonicalSQLite

DeletionStatus = Literal["pending", "incomplete", "complete"]


@dataclass(frozen=True, slots=True)
class ManagedDeletionOperation:
    operation_id: str
    creator_account_id: str
    deletion_revision: int
    status: DeletionStatus


class ManagedDeletionOperations:
    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database

    def get(self, creator_account_id: str, operation_id: str) -> ManagedDeletionOperation | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM managed_deletion_operations
                   WHERE creator_account_id=? AND operation_id=?""",
                (creator_account_id, operation_id),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "complete":
                work = connection.execute(
                    """SELECT completed_at FROM projection_work
                       WHERE creator_account_id=? AND work_kind='reseed'
                         AND conversation_id IS NULL AND created_at=?
                       ORDER BY work_id DESC LIMIT 1""",
                    (creator_account_id, row["requested_at"]),
                ).fetchone()
                if work is not None and work["completed_at"] is not None:
                    now = str(work["completed_at"])
                    connection.execute(
                        """UPDATE managed_deletion_operations
                           SET status='complete',last_error_code=NULL,updated_at=?
                           WHERE operation_id=?""",
                        (now, operation_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM managed_deletion_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
            assert row is not None
            return self._operation(row)

    def outstanding(self, creator_account_id: str) -> ManagedDeletionOperation | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT operation_id FROM managed_deletion_operations
                   WHERE creator_account_id=? AND status!='complete'
                   ORDER BY requested_at DESC LIMIT 1""",
                (creator_account_id,),
            ).fetchone()
        if row is None:
            return None
        operation = self.get(creator_account_id, str(row[0]))
        return None if operation is not None and operation.status == "complete" else operation

    def mark_incomplete(
        self, creator_account_id: str, operation_id: str, error_code: str
    ) -> ManagedDeletionOperation:
        if not error_code or len(error_code) > 64:
            raise ValueError("deletion error code must be 1-64 characters")
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM managed_deletion_operations
                   WHERE creator_account_id=? AND operation_id=?""",
                (creator_account_id, operation_id),
            ).fetchone()
            if row is None:
                raise LookupError(operation_id)
            if row["status"] != "complete":
                connection.execute(
                    """UPDATE managed_deletion_operations
                       SET status='incomplete',last_error_code=?,updated_at=?
                       WHERE operation_id=?""",
                    (
                        error_code,
                        datetime.now(timezone.utc).isoformat(),
                        operation_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM managed_deletion_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            assert row is not None
            return self._operation(row)

    @staticmethod
    def _operation(row) -> ManagedDeletionOperation:
        return ManagedDeletionOperation(
            operation_id=str(row["operation_id"]),
            creator_account_id=str(row["creator_account_id"]),
            deletion_revision=int(row["deletion_revision"]),
            status=str(row["status"]),  # type: ignore[arg-type]
        )
