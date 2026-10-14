"""Brain-only browser-ceremony journal; browser returns are invalidations only."""
from __future__ import annotations

import asyncio
import json

from app.onboarding.contract import canonical
from app.persistence.onboarding import require_journey
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.browser_ceremony import PROFILE, PROOF_PROFILE, browser_entry_url, expected_completion
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp


class BrowserCeremonies:
    def __init__(self, store, client, *, hosted_origin, decision_client=None, authentication_client=None,
                 authentication_providers=("default",)):
        self.store, self.client, self.hosted_origin = store, client, hosted_origin
        from app.onboarding.decisions import DesktopDecisions
        self.decisions = None if decision_client is None else DesktopDecisions(store, self, decision_client)
        from app.onboarding.authentication import DesktopAuthentication
        self.authentication = None if authentication_client is None else DesktopAuthentication(
            store, self, authentication_client, providers=authentication_providers)
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()

    def row(self, operation_id):
        require_journey(operation_id)
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (operation_id,)).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def request(row, *, known=True):
        result = {"profile": PROFILE, "operation_id": row["hosted_operation_id"], "binding": json.loads(row["binding_json"])}
        if known and row["ceremony_id"] is not None:
            result["ceremony_id"] = row["ceremony_id"]
        return result

    def _reserve(self, operation_id, binding, authorize):
        from app.persistence.auth import _new_uuid7
        encoded = canonical(binding).decode()
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            row = connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (operation_id,)).fetchone()
            if row is not None:
                if row["binding_json"] != encoded:
                    raise HostedGrantUnavailable("Browser ceremony selection changed")
                return dict(row), False
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
            expected = {"account.connect": "connect-account", "activation.review": "activate-complimentary"}
            if (operation is None or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or expected.get(operation["action"]) != binding["purpose"]
                    or operation["expires_at"] <= self.store._now().timestamp()
                    or timestamp(binding["source_expires_at"]) <= self.store._now()):
                raise HostedGrantUnavailable("Browser ceremony admission is unavailable")
            hosted_operation = _new_uuid7(self.store._now())
            request = {"profile": PROFILE, "operation_id": hosted_operation, "binding": binding}
            self.client.contract.validate("proof-challenge-request", {"profile": PROOF_PROFILE,
                "target": {"operation": "browser-ceremony-prepare", "request": request}})
            connection.execute("INSERT INTO desktop_browser_ceremonies (operation_id,hosted_operation_id,binding_json) VALUES (?,?,?)",
                               (operation_id, hosted_operation, encoded))
            return dict(connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (operation_id,)).fetchone()), True

    async def prepare(self, operation_id, binding, *, authorize):
        async with self._lock:
            row, fresh = self._reserve(operation_id, binding, authorize)
            if not fresh:
                return await self._read(row, authorize=authorize)
            # The exact request is durable before the single prepare send. A
            # restart or lost response can only read this operation.
            result = await self._effects.run(self.client.prepare, self.request(row, known=False),
                before_send=authorize, journey_id=self._journey(operation_id))
            self._bind(row, result, preparing=True)
            return result

    async def reconcile(self, operation_id, *, authorize):
        async with self._lock:
            row = self.row(operation_id)
            if row is None:
                raise HostedGrantUnavailable("Browser ceremony outcome is unavailable")
            return await self._read(row, authorize=authorize)

    async def _read(self, row, *, authorize):
        result = await self._effects.run(self.client.read, self.request(row), expires_at=row["provider_expires_at"],
            before_send=authorize, journey_id=self._journey(row["operation_id"]), protected=False)
        if result["status"] == "found":
            self._bind(row, result)
        return result

    async def retire(self, operation_id, *, authorize):
        async with self._lock:
            row = self.row(operation_id)
            if row is None:
                raise HostedGrantUnavailable("Browser ceremony outcome is unavailable")
            if row["ceremony_id"] is None:
                result = await self._read(row, authorize=authorize)
                if result["status"] != "found":
                    return result
                row = self.row(operation_id)
            result = await self._effects.run(self.client.cancel, self.request(row), expires_at=row["provider_expires_at"],
                before_send=authorize, journey_id=self._journey(operation_id))
            if result["status"] == "found":
                self._bind(row, result)
            return result

    def _journey(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT journey_id FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise HostedGrantUnavailable("Browser ceremony owner is unavailable")
        return row["journey_id"]

    def _bind(self, row, result, *, preparing=False):
        # Domain results (including CLR1) are intentionally not serialized here.
        # The caller immediately delegates them to their existing owning service.
        view = result if preparing else result["snapshot"]
        entry = result if preparing else result.get("entry")
        revision = None if preparing else view["revision"]
        with self.store.database.transaction() as connection:
            current = connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (row["operation_id"],)).fetchone()
            if (current is None or current["binding_json"] != row["binding_json"]
                    or (current["provider_revision"] is not None and (revision is None or revision < current["provider_revision"]))):
                raise HostedGrantUnavailable("Browser ceremony result changed")
            state = "active" if preparing else view["presentation_state"]
            status = "pending" if preparing else view["domain_status"]
            reference = None if entry is None or state != "active" else entry["entry_reference"]
            # Ceremony revision fences workflow writes. Deadline expiry and the
            # independently owned source projection can change at that revision.
            # Reads are serialized and still verify the exact immutable binding.
            connection.execute("UPDATE desktop_browser_ceremonies SET ceremony_id=?,provider_expires_at=?,entry_reference=?,"
                "presentation_state=?,domain_status=?,provider_revision=? WHERE operation_id=?",
                (view["ceremony_id"], view["expires_at"], reference, state, status, revision, row["operation_id"]))
        events.publish()

    def browser_entry(self, operation_id):
        """Read only nonauthorizing launch coordinates for the exact task."""
        if self.authentication is not None and not self.authentication.can_open(operation_id):
            return None
        row = self.task_row(operation_id)
        if (row is None or row["entry_reference"] is None or row["presentation_state"] != "active"
                or timestamp(row["provider_expires_at"]) <= self.store._now()):
            return None
        binding = json.loads(row["binding_json"])
        result = {"entry_url": browser_entry_url(self.hosted_origin, row["entry_reference"]),
            "ceremony_id": row["ceremony_id"], "attempt_id": binding["attempt_id"],
            "completion": expected_completion(binding), "expires_at": row["provider_expires_at"]}
        if (binding["purpose"] == "activate-complimentary" and self.authentication is not None
                and self.authentication.preparation_recovery(operation_id)):
            result["preparation_recovery"] = True
        return result

    def task_row(self, operation_id):
        parent = operation_id if self.authentication is None else self.authentication.parent_operation(operation_id)
        return self.row(parent)

    async def stop(self):
        if self.authentication is not None:
            await self.authentication.stop()
        if self.decisions is not None:
            await self.decisions.stop()
        self._effects.close_admission()
        await self._effects.drain()
        close = getattr(self.client.transport, "close", None)
        if close is not None:
            close()
