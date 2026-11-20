"""Local display journal; only explicit desktop commands submit customer consent."""
from __future__ import annotations

import json

from app.onboarding import timing
from contextlib import nullcontext

from app.onboarding.contract import canonical
from app.onboarding.coordinator import OperationResult
from app.onboarding.held_reads import HeldReads
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.desktop_decision import PROFILE, DesktopDecisionRefused
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp


class DesktopDecisions:
    def __init__(self, store, ceremonies, client):
        self.store, self.ceremonies, self.client = store, ceremonies, client
        self._effects = OwnerEffects()
        self._held = HeldReads()

    def _projection(self, journey_id, action, *, connection=None):
        """Read only a still-current local display, never contact Provider."""
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            rows = current.execute(
                "SELECT d.operation_id,d.projection_json FROM desktop_decision_projections d "
                "JOIN desktop_setup_operations o ON o.operation_id=d.operation_id "
                "WHERE o.journey_id=? AND o.action=? AND o.state IN ('accepted','waiting','outcome_unknown')",
                (journey_id, action)).fetchall()
            if len(rows) != 1:
                return None
            projection = json.loads(rows[0]["projection_json"])
            parent = rows[0]["operation_id"]
            ceremony = self.ceremonies.row(parent, connection=current)
            if (ceremony is None or projection["operation_id"] != ceremony["hosted_operation_id"]
                    or projection["ceremony_id"] != ceremony["ceremony_id"]
                    or self.ceremonies.replacement_reservation(parent, ceremony["hosted_operation_id"], connection=current) is not None):
                return None
            return {"operation_id": parent, "projection": projection}

    def status(self, journey_id, action, *, connection=None):
        selected = self._projection(journey_id, action, connection=connection)
        if selected is None:
            return None
        projection = selected["projection"]
        state = projection["state"]
        if state == "confirmation-required" and timestamp(projection["decision"]["expires_at"]) <= self.store._now():
            state = "unavailable"
        return {"operation_id": selected["operation_id"], "state": state}

    def presentation(self, journey_id, action, *, connection=None):
        selected = self._projection(journey_id, action, connection=connection)
        if selected is None:
            return None
        projection = selected["projection"]
        if (projection["state"] != "confirmation-required"
                or timestamp(projection["decision"]["expires_at"]) <= self.store._now()):
            return None
        return selected

    def _request(self, operation_id, *, connection=None):
        row = self.ceremonies.row(operation_id, connection=connection)
        if row is None or row["ceremony_id"] is None:
            raise HostedGrantUnavailable("Decision scope is unavailable")
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            authentication = connection.execute("SELECT revision FROM desktop_authentication_projections WHERE operation_id=?",
                (operation_id,)).fetchone()
        row["authentication_revision"] = 0 if authentication is None else authentication["revision"]
        return row, {"profile": PROFILE, "operation_id": row["hosted_operation_id"],
            "ceremony_id": row["ceremony_id"], "binding": json.loads(row["binding_json"])}

    def _save(self, row, projection):
        encoded = canonical(projection).decode()
        with self.store.database.transaction() as connection:
            current = self.ceremonies.require_current(row, connection=connection)
            if current["provider_revision"] != row["provider_revision"]:
                return
            if projection["state"] == "confirmation-required":
                authentication = connection.execute("SELECT revision FROM desktop_authentication_projections WHERE operation_id=?",
                    (row["operation_id"],)).fetchone()
                revision = 0 if authentication is None else authentication["revision"]
                if revision != row["authentication_revision"]:
                    return
            previous = connection.execute("SELECT projection_json,provider_revision FROM desktop_decision_projections WHERE operation_id=?",
                (row["operation_id"],)).fetchone()
            if previous is not None and previous["projection_json"] == encoded:
                return
            if (previous is not None and previous["provider_revision"] == row["provider_revision"]
                    and json.loads(previous["projection_json"])["state"] == "accepted"):
                return
            connection.execute("INSERT INTO desktop_decision_projections(operation_id,projection_json,provider_revision) VALUES (?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET projection_json=excluded.projection_json,provider_revision=excluded.provider_revision",
                (row["operation_id"], encoded, row["provider_revision"]))
        events.publish()

    def hold_read(self, operation_id, *, authorize, connection=None):
        """Start fetching this decision's next read challenge, so a following refresh sends at once."""
        if not hasattr(self.client, "prefetch_read"):
            return False
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            try:
                row, request = self._request(operation_id, connection=connection)
            except HostedGrantUnavailable:
                return False
            journey = self.ceremonies._journey(operation_id, connection=connection)
        return self._held.hold(row["hosted_operation_id"], lambda: self._effects.run(self.client.prefetch_read, request,
            before_send=self.ceremonies.guarded(row, authorize), journey_id=journey, protected=False))

    async def refresh(self, operation_id, *, authorize):
        row, request = self._request(operation_id)
        ticket = await self._held.take(row["hosted_operation_id"])
        options = {} if ticket is None else {"prefetched": ticket}
        with timing.phase("decision-read", "read-end"):
            result = await self._effects.run(self.client.read, request,
                expires_at=row["provider_expires_at"], before_send=self.ceremonies.guarded(row, authorize),
                journey_id=self.ceremonies._journey(operation_id), protected=False, **options)
        self._save(row, result)
        return result

    async def execute(self, operation, *, authorize, read_guard, reconcile):
        selected = json.loads(operation["owner_scope_json"])["desktop_decision"]
        if selected is None:
            return OperationResult("refused", "operation_refused")
        parent = selected["operation_id"]
        row, request = self._request(parent)
        expected = {"account.confirm": "connect-account", "activation.confirm": "activate-complimentary"}
        projection = selected["projection"]
        if (self.ceremonies._journey(parent) != operation["journey_id"]
                or request["binding"]["purpose"] != expected[operation["action"]]
                or projection["operation_id"] != request["operation_id"]
                or projection["ceremony_id"] != request["ceremony_id"]):
            raise HostedGrantUnavailable("Decision selection changed")
        if reconcile:
            # Restart and uncertain replies can only read the original owner
            # operation. A renewed explicit click is needed to submit again.
            result = await self.refresh(parent, authorize=read_guard)
        else:
            authorize()
            if (self.store._now().timestamp() >= operation["expires_at"]
                    or timestamp(projection["decision"]["expires_at"]) <= self.store._now()):
                return OperationResult("refused", "operation_expired")
            request.update(decision_reference=projection["decision_reference"],
                decision_digest=projection["decision_digest"], consent=projection["decision"]["action"])
            try:
                result = await self._effects.run(self.client.submit, request,
                    expires_at=row["provider_expires_at"], before_send=self.ceremonies.guarded(row, authorize, mutation=True),
                    journey_id=operation["journey_id"])
                self._save(row, result)
            except DesktopDecisionRefused:
                await self.refresh(parent, authorize=read_guard)
                return OperationResult("refused", "operation_refused")
        if result["state"] == "accepted":
            return OperationResult("completed")
        return OperationResult("refused", "operation_unconfirmed" if reconcile else "operation_refused")

    async def stop(self):
        self._held.close()
        self._effects.close_admission()
        await self._effects.drain()
        close = getattr(self.client.transport, "close", None)
        if close is not None:
            close()
