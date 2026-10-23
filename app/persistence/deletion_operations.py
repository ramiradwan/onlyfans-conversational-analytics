"""Durable truthfulness for whole-Vault deletion completion."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
import json

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
                "SELECT * FROM managed_deletion_operations WHERE creator_account_id=? AND operation_id=?",
                (creator_account_id, operation_id),
            ).fetchone()
            if row is None:
                return None
            work = connection.execute(
                "SELECT completed_at FROM projection_work WHERE creator_account_id=? "
                "AND work_kind='reseed' AND conversation_id IS NULL AND created_at=? ORDER BY work_id DESC LIMIT 1",
                (creator_account_id, row["requested_at"]),
            ).fetchone()
            ack = connection.execute(
                "SELECT 1 FROM derived_deletion_acknowledgements WHERE operation_id=? "
                "AND creator_account_id=? AND deletion_revision=?",
                (operation_id, creator_account_id, row["deletion_revision"]),
            ).fetchone()
            ready = bool(
                work is not None
                and work["completed_at"] is not None
                and ack is not None
            )
            if ready:
                connection.execute(
                    "UPDATE managed_deletion_operations SET status='complete',last_error_code=NULL,updated_at=? WHERE operation_id=?",
                    (str(work["completed_at"]), operation_id),
                )
            elif row["status"] == "complete":
                connection.execute(
                    "UPDATE managed_deletion_operations SET status='incomplete',last_error_code='derived_receipt_missing' WHERE operation_id=?",
                    (operation_id,),
                )
            row = connection.execute(
                "SELECT * FROM managed_deletion_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
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

    def pending_accounts(self, *, limit=4):
        with self.database.transaction() as db:
            accounts = [
                row[0]
                for row in db.execute(
                    "SELECT w.creator_account_id FROM derived_deletion_work w "
                    "WHERE completed_revision<target_revision OR EXISTS(SELECT 1 FROM managed_deletion_operations o "
                    "WHERE o.creator_account_id=w.creator_account_id AND o.status!='complete') "
                    "ORDER BY attempted_at,w.creator_account_id LIMIT ?",
                    (limit,),
                )
            ]
            now = datetime.now(timezone.utc).isoformat()
            db.executemany(
                "UPDATE derived_deletion_work SET attempted_at=? WHERE creator_account_id=?",
                [(now, account) for account in accounts],
            )
            return accounts

    def target(self, account_id):
        with self.database.read() as db:
            row = db.execute(
                "SELECT target_revision FROM derived_deletion_work WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            wide = db.execute(
                "SELECT b.deletion_revision FROM deletion_barriers b JOIN derived_deletion_work w USING(creator_account_id) "
                "WHERE b.creator_account_id=? AND b.scope_kind='account' AND b.scope_key='*' AND b.deletion_revision>w.completed_revision",
                (account_id,),
            ).fetchone()
            return row[0], wide[0] if wide else 0

    def acknowledge(self, account_id, target_revision, store_identity, receipt):
        if not store_identity or not receipt:
            raise ValueError("deletion_receipt_invalid")
        with self.database.transaction() as db:
            work = db.execute(
                "SELECT * FROM derived_deletion_work WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            if work is None or work["target_revision"] != target_revision:
                return False
            value = json.loads(receipt)
            if (
                value.get("target_revision") != target_revision
                or value.get("store_identity") != store_identity
                or type(value.get("account_revision")) is not int
            ):
                raise ValueError("deletion_receipt_invalid")
            for row in db.execute(
                "SELECT * FROM managed_deletion_operations WHERE creator_account_id=? AND deletion_revision<=?",
                (account_id, target_revision),
            ):
                old = db.execute(
                    "SELECT * FROM derived_deletion_acknowledgements WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                if old is None:
                    if row["deletion_revision"] > value["account_revision"]:
                        return False
                    db.execute(
                        "INSERT INTO derived_deletion_acknowledgements VALUES(?,?,?,?,?)",
                        (
                            row["operation_id"],
                            account_id,
                            row["deletion_revision"],
                            store_identity,
                            receipt,
                        ),
                    )
            db.execute(
                "UPDATE derived_deletion_work SET completed_revision=? WHERE creator_account_id=? AND target_revision=?",
                (target_revision, account_id, target_revision),
            )
        return True

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
