"""Join durable deletion components without a cross-database transaction."""

import asyncio
import json
import logging
import time

LOGGER = logging.getLogger(__name__)


class DerivedDeletionService:
    def __init__(
        self,
        operations,
        *,
        project_committed,
        reconcile_findings,
        max_steps=16,
        budget_seconds=2.0,
        monotonic=time.monotonic
    ):
        self.operations = operations
        self.project_committed = project_committed
        self.reconcile_findings = reconcile_findings
        self.max_steps, self.budget_seconds, self.monotonic = (
            max_steps,
            budget_seconds,
            monotonic,
        )

    def reconcile(self, account_id):
        target = self.operations.target(account_id)
        if target is None:
            return True
        revision, wide = target
        deadline = self.monotonic() + self.budget_seconds
        receipt = None

        def acknowledge_absence(value):
            return self.operations.acknowledge(
                account_id,
                revision,
                value["store_identity"],
                json.dumps(value, sort_keys=True, separators=(",", ":")),
            )

        for _ in range(self.max_steps):
            if (
                self.monotonic() >= deadline
                or self.operations.target(account_id) != target
            ):
                return False
            try:
                receipt = self.reconcile_findings(
                    account_id,
                    revision,
                    account_wide=wide,
                    deadline=deadline,
                    acknowledge_absence=acknowledge_absence,
                )
            except ValueError as error:
                if str(error) == "nlp_findings_deadline":
                    return False
                raise
            if receipt is not None:
                break
        if receipt is None:
            return False
        from app.analytics.opaque_refs import account_ref

        if (
            receipt.get("account_ref") != account_ref(account_id)
            or receipt.get("target_revision") != revision
        ):
            raise ValueError("deletion_receipt_target_invalid")
        if (
            type(receipt.get("account_revision")) is not int
            or receipt["account_revision"] < wide
        ):
            raise ValueError("deletion_receipt_scope_invalid")
        return self.operations.acknowledge(
            account_id,
            revision,
            receipt["store_identity"],
            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        )

    async def finish(self, account_id, operation_id=None):
        failed = False
        try:
            await self.project_committed(account_id)
        except Exception:
            failed = True
        try:
            from app.services.retention_maintenance import _owned

            complete = await _owned(asyncio.to_thread(self.reconcile, account_id))
        except Exception:
            complete = False
            LOGGER.warning(
                "derived_deletion_event status=incomplete reason_code=findings_cleanup_failed"
            )
        if operation_id is None:
            operation = self.operations.outstanding(account_id)
            operation_id = operation.operation_id if operation else None
        if operation_id is None:
            return None
        if failed or not complete:
            return self.operations.mark_incomplete(
                account_id,
                operation_id,
                (
                    "dependent_state_cleanup_failed"
                    if failed
                    else "derived_cleanup_pending"
                ),
            )
        return self.operations.get(account_id, operation_id)

    async def run_pending(self):
        for account_id in self.operations.pending_accounts():
            await self.finish(account_id)
