"""Brain-only browser-ceremony journal; browser returns are invalidations only."""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import nullcontext

from app.onboarding import timing
from app.onboarding.contract import canonical
from app.onboarding.held_reads import HeldReads
from app.persistence.onboarding import require_journey
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.browser_ceremony import PROFILE, PROOF_PROFILE, binding_digest, browser_entry_url, expected_completion
from app.security.browser_ceremony_replacement import PROFILE as REPLACEMENT_PROFILE, PROOF_PROFILE as REPLACEMENT_PROOF_PROFILE
from app.security.browser_ceremony_replacement import _contract as replacement_contract
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp, valid_uuid7


class BrowserCeremonies:
    def __init__(self, store, client, *, hosted_origin, decision_client=None, authentication_client=None,
                 authentication_providers=("default",), wait_client=None):
        self.store, self.client, self.hosted_origin = store, client, hosted_origin
        from app.onboarding.decisions import DesktopDecisions
        self.decisions = None if decision_client is None else DesktopDecisions(store, self, decision_client)
        from app.onboarding.authentication import DesktopAuthentication
        self.authentication = None if authentication_client is None else DesktopAuthentication(
            store, self, authentication_client, providers=authentication_providers, waits=wait_client)
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()
        self._held = HeldReads()

    def hold_read(self, operation_id, *, authorize, connection=None):
        """Start fetching this ceremony's next read challenge, so a following read sends at once."""
        if not hasattr(self.client, "prefetch_read"):
            return False
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            row = self.row(operation_id, connection=connection)
            if row is None or row["ceremony_id"] is None:
                return False
            journey = self._journey(operation_id, connection=connection)
        return self._held.hold(operation_id, lambda: self._effects.run(self.client.prefetch_read, self.request(row),
            before_send=authorize, journey_id=journey, protected=False))

    def row(self, operation_id, *, connection=None):
        require_journey(operation_id)
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
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
                return dict(row), False, None
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
            return (dict(connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (operation_id,)).fetchone()),
                    True, operation["journey_id"])

    def replacement_reservation(self, operation_id, predecessor_operation_id, *, connection=None):
        """Return the retained nonauthorizing request, including after a restart."""
        require_journey(operation_id)
        if not valid_uuid7(predecessor_operation_id):
            raise ValueError("Invalid predecessor operation reference")
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            row = connection.execute(
                "SELECT request_json FROM desktop_browser_ceremony_replacements "
                "WHERE parent_operation_id=? AND predecessor_operation_id=?",
                (operation_id, predecessor_operation_id)).fetchone()
        return None if row is None else json.loads(row["request_json"])

    def reserve_replacement(self, operation_id, predecessor, *, authorize):
        """Reserve an exact ordinary successor; Provider still decides admission.

        The caller must be an explicitly admitted current owner command. This
        reservation does not send a proof, retire a task, or expose a successor.
        Never infer noncommit from local expiry or an unavailable read.
        """
        from app.persistence.auth import _new_uuid7
        require_journey(operation_id)
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            row = self.row(operation_id, connection=connection)
            operation = connection.execute(
                "SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
            now = self.store._now()
            if (row is None or operation is None
                    or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or operation["action"] not in {"account.connect", "activation.review"}
                    or operation["expires_at"] <= now.timestamp()
                    or row["presentation_state"] not in {"retired", "expired"}
                    or row["domain_status"] not in {"pending", "awaiting-authentication", "awaiting-confirmation"}
                    or row["ceremony_id"] is None or row["provider_revision"] is None):
                raise HostedGrantUnavailable("Browser ceremony replacement is unavailable")
            binding = json.loads(row["binding_json"])
            if timestamp(binding["source_expires_at"]) <= now:
                raise HostedGrantUnavailable("Browser ceremony replacement source expired")
            expected = {"predecessor_operation_id": row["hosted_operation_id"],
                "predecessor_ceremony_id": row["ceremony_id"],
                "predecessor_binding_digest": binding_digest(binding),
                "predecessor_revision": row["provider_revision"]}
            if predecessor != expected:
                raise HostedGrantUnavailable("Browser ceremony replacement predecessor changed")
            saved = self.replacement_reservation(operation_id, row["hosted_operation_id"], connection=connection)
            if saved is not None:
                if any(saved[name] != value for name, value in expected.items()):
                    raise HostedGrantUnavailable("Browser ceremony replacement predecessor changed")
                return saved
            request = {"profile": REPLACEMENT_PROFILE, **expected,
                "successor_operation_id": _new_uuid7(now), "successor_attempt_id": secrets.token_urlsafe(32)}
            replacement_contract().validate("proof-challenge-request", {"profile": REPLACEMENT_PROOF_PROFILE,
                "target": {"operation": "browser-ceremony-replace", "request": request}})
            connection.execute(
                "INSERT INTO desktop_browser_ceremony_replacements "
                "(predecessor_operation_id,parent_operation_id,successor_operation_id,request_json,source_expires_at,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (row["hosted_operation_id"], operation_id, request["successor_operation_id"],
                 canonical(request).decode(), binding["source_expires_at"], now.timestamp()))
        return request

    async def prepare(self, operation_id, binding, *, authorize):
        async with self._lock:
            # Store transactions run off the event loop, each awaited before the step that depends on it.
            row, fresh, journey = await asyncio.to_thread(self._reserve, operation_id, binding, authorize)
            if not fresh:
                return await self._read(row, authorize=authorize)
            # The exact request is durable before the single prepare send. A
            # restart or lost response can only read this operation.
            result = await self._effects.run(self.client.prepare, self.request(row, known=False),
                before_send=authorize, journey_id=journey)
            with timing.phase("ceremony-prepare", "persist"):
                await asyncio.to_thread(self._bind, row, result, preparing=True)
            return result

    async def reconcile(self, operation_id, *, authorize):
        async with self._lock:
            row = self.row(operation_id)
            if row is None:
                raise HostedGrantUnavailable("Browser ceremony outcome is unavailable")
            return await self._read(row, authorize=authorize)

    async def _read(self, row, *, authorize):
        ticket = await self._held.take(row["operation_id"])
        options = {} if ticket is None else {"prefetched": ticket}
        with timing.phase("ceremony-read", "read-end"):
            result = await self._effects.run(self.client.read, self.request(row), expires_at=row["provider_expires_at"],
                before_send=authorize, journey_id=self._journey(row["operation_id"]), protected=False, **options)
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

    def _journey(self, operation_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
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
        # Each helper payload read asks for this, so it opens the store once.
        with self.store.database.read() as connection:
            if self.authentication is not None and not self.authentication.can_open(operation_id, connection=connection):
                return None
            row = self.task_row(operation_id, connection=connection)
            if (row is None or row["entry_reference"] is None or row["presentation_state"] != "active"
                    or timestamp(row["provider_expires_at"]) <= self.store._now()):
                return None
            binding = json.loads(row["binding_json"])
            recovery = (binding["purpose"] == "activate-complimentary" and self.authentication is not None
                        and self.authentication.preparation_recovery(operation_id, connection=connection))
        result = {"entry_url": browser_entry_url(self.hosted_origin, row["entry_reference"]),
            "ceremony_id": row["ceremony_id"], "attempt_id": binding["attempt_id"],
            "completion": expected_completion(binding), "expires_at": row["provider_expires_at"]}
        if recovery:
            result["preparation_recovery"] = True
        return result

    def task_row(self, operation_id, *, connection=None):
        parent = (operation_id if self.authentication is None
                  else self.authentication.parent_operation(operation_id, connection=connection))
        return self.row(parent, connection=connection)

    async def stop(self):
        # The proof clients can share one transport; drain all of them before any close.
        owners = tuple(owner for owner in (self.authentication, self.decisions) if owner is not None)
        self._held.close()
        for effects in (*(owner._effects for owner in owners), self._effects):
            effects.close_admission()
        for effects in (*(owner._effects for owner in owners), self._effects):
            await effects.drain()
        for owner in owners:
            await owner.stop()
        close = getattr(self.client.transport, "close", None)
        if close is not None:
            close()
