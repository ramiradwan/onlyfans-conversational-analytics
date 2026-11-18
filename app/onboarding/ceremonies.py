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
from app.security.browser_ceremony import PROFILE, PROOF_PROFILE, BrowserCeremonyRefused, binding_digest, browser_entry_url, expected_completion
from app.security.browser_ceremony_replacement import PROFILE as REPLACEMENT_PROFILE, PROOF_PROFILE as REPLACEMENT_PROOF_PROFILE
from app.security.browser_ceremony_replacement import _contract as replacement_contract
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp, valid_uuid7


class BrowserCeremonies:
    def __init__(self, store, client, *, hosted_origin, decision_client=None, authentication_client=None,
                 authentication_providers=("default",), wait_client=None, replacement_client=None):
        self.store, self.client, self.hosted_origin = store, client, hosted_origin
        self.replacement_client = replacement_client
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
        return self._held.hold(row["hosted_operation_id"], lambda: self._effects.run(self.client.prefetch_read, self.request(row),
            before_send=self.guarded(row, authorize), journey_id=journey, protected=False))

    def row(self, operation_id, *, connection=None):
        require_journey(operation_id)
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            rows = connection.execute("SELECT * FROM desktop_browser_ceremony_current WHERE operation_id=?", (operation_id,)).fetchall()
        if len(rows) > 1:
            raise HostedGrantUnavailable("Browser ceremony lineage is ambiguous")
        return None if not rows else dict(rows[0])

    def require_current(self, row, *, connection=None, mutation=False):
        """Fence held reads and protected effects against the exact ceremony tip."""
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            current = self.row(row["operation_id"], connection=connection)
            if (current is None or any(current[name] != row[name] for name in (
                    "hosted_operation_id", "binding_json", "ceremony_id"))):
                raise HostedGrantUnavailable("Browser ceremony lineage changed")
            if mutation:
                # The current child and Provider decision own their deadlines.
                # A retired local presentation must not shorten a live confirmation.
                parent = connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
                    (row["operation_id"],)).fetchone()
                if (parent is None or parent["state"] not in {"accepted", "waiting", "outcome_unknown"}
                        or self.replacement_reservation(row["operation_id"], row["hosted_operation_id"], connection=connection) is not None):
                    raise HostedGrantUnavailable("Browser ceremony mutation is unavailable")
        return current

    def guarded(self, row, authorize, *, mutation=False):
        def guard():
            # The existing owner guard and the lineage check share one
            # authenticated connection on the effect thread.
            with self.store.database.shared_reads():
                authorize()
                self.require_current(row, mutation=mutation)
        return guard

    def is_successor(self, row, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            return connection.execute("SELECT 1 FROM desktop_browser_ceremony_successors WHERE hosted_operation_id=?",
                (row["hosted_operation_id"],)).fetchone() is not None

    def matches_request(self, row, request):
        return (row is not None and row["hosted_operation_id"] == request["operation_id"]
            and row["ceremony_id"] == request["ceremony_id"]
            and json.loads(row["binding_json"]) == request["binding"])

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
            row = self.row(operation_id, connection=connection)
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

    def replacement_offer(self, journey_id, *, connection):
        """Local display evidence only: one retained, inactive account ceremony."""
        if self.replacement_client is None or self.authentication is None or journey_id is None:
            return None
        rows = connection.execute("SELECT * FROM desktop_setup_operations WHERE journey_id=? "
            "AND action='account.connect' AND state IN ('accepted','waiting','outcome_unknown')",
            (journey_id,)).fetchall()
        if len(rows) != 1:
            return None
        parent = dict(rows[0])
        try:
            predecessor = self._eligible_predecessor(parent["operation_id"], connection=connection,
                allow_expired_parent=True)
        except HostedGrantUnavailable:
            return None
        if self.replacement_reservation(parent["operation_id"], predecessor["predecessor_operation_id"],
                connection=connection) is not None:
            return None
        # Do not compete with another accepted child, including one whose
        # authentication outcome is still being reconciled.
        if connection.execute("SELECT 1 FROM desktop_setup_operations WHERE journey_id=? AND "
                "action IN ('auth.select','auth.switch','auth.cancel') AND state IN "
                "('accepted','waiting','outcome_unknown')", (journey_id,)).fetchone() is not None:
            return None
        return {"parent_operation_id": parent["operation_id"], "predecessor": predecessor}

    def _eligible_predecessor(self, operation_id, *, connection, allow_expired_parent=False):
        row = self.row(operation_id, connection=connection)
        operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (operation_id,)).fetchone()
        now = self.store._now()
        if (row is None or operation is None
                or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}
                or operation["action"] not in {"account.connect", "activation.review"}
                or (not allow_expired_parent and operation["expires_at"] <= now.timestamp())
                or row["presentation_state"] not in {"retired", "expired"}
                or row["domain_status"] not in {"pending", "awaiting-authentication", "awaiting-confirmation"}
                or row["ceremony_id"] is None or row["provider_revision"] is None):
            raise HostedGrantUnavailable("Browser ceremony replacement is unavailable")
        pending = connection.execute("SELECT 1 FROM desktop_setup_operations WHERE operation_id<>? "
            "AND state IN ('accepted','waiting','outcome_unknown') "
            "AND json_extract(owner_scope_json,'$.desktop_decision.operation_id')=? LIMIT 1",
            (operation_id, operation_id)).fetchone()
        decision = connection.execute("SELECT projection_json FROM desktop_decision_projections WHERE operation_id=?",
            (operation_id,)).fetchone()
        projection = None if decision is None else json.loads(decision[0])
        if (pending is not None or projection is not None and (projection["state"] == "accepted"
                or projection["state"] == "confirmation-required" and timestamp(projection["decision"]["expires_at"]) > now)):
            raise HostedGrantUnavailable("Browser ceremony decision must be reconciled")
        binding = json.loads(row["binding_json"])
        if timestamp(binding["source_expires_at"]) <= now:
            raise HostedGrantUnavailable("Browser ceremony replacement source expired")
        return {"predecessor_operation_id": row["hosted_operation_id"],
            "predecessor_ceremony_id": row["ceremony_id"],
            "predecessor_binding_digest": binding_digest(binding),
            "predecessor_revision": row["provider_revision"]}

    def reserve_replacement(self, operation_id, predecessor, *, authorize, allow_expired_parent=False):
        """Reserve an exact ordinary successor; Provider still decides admission.

        The caller must be an explicitly admitted current owner command. This
        reservation does not send a proof, retire a task, or expose a successor.
        Never infer noncommit from local expiry or an unavailable read.
        """
        from app.persistence.auth import _new_uuid7
        require_journey(operation_id)
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            expected = self._eligible_predecessor(operation_id, connection=connection,
                allow_expired_parent=allow_expired_parent)
            row = self.row(operation_id, connection=connection)
            binding = json.loads(row["binding_json"])
            now = self.store._now()
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

    @staticmethod
    def _replacement_binding(row, request):
        return {**json.loads(row["binding_json"]), "attempt_id": request["successor_attempt_id"]}

    def _replacement_current(self, operation_id, request, *, connection=None, for_send=False, allow_expired_parent=False):
        """Only the original retained predecessor may send or adopt its reserved successor."""
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            row = self.row(operation_id, connection=connection)
            saved = self.replacement_reservation(operation_id, request["predecessor_operation_id"], connection=connection)
            parent = connection.execute("SELECT state,expires_at FROM desktop_setup_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if (row is None or saved != request or parent is None
                    or parent["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or (for_send and not allow_expired_parent and parent["expires_at"] <= self.store._now().timestamp())
                    or row["hosted_operation_id"] != request["predecessor_operation_id"]
                    or row["ceremony_id"] != request["predecessor_ceremony_id"]
                    or binding_digest(json.loads(row["binding_json"])) != request["predecessor_binding_digest"]):
                raise HostedGrantUnavailable("Browser ceremony replacement lineage changed")
        return row

    async def replace(self, operation_id, predecessor, *, authorize, allow_expired_parent=False):
        """An explicit owner command may send this exact reserved request once per invocation.

        Reconciliation is separate and read-only. There is no automatic resend,
        source renewal, or implicit authentication.
        """
        if self.replacement_client is None:
            raise HostedGrantUnavailable("Browser ceremony replacement is unavailable")
        async with self._lock:
            request = await asyncio.to_thread(self.reserve_replacement, operation_id, predecessor, authorize=authorize,
                allow_expired_parent=allow_expired_parent)
            row = self._replacement_current(operation_id, request)
            def guard():
                authorize()
                current = self._replacement_current(operation_id, request, for_send=True,
                    allow_expired_parent=allow_expired_parent)
                with self.store.database.read() as connection:
                    expected = self._eligible_predecessor(operation_id, connection=connection,
                        allow_expired_parent=allow_expired_parent)
                    if expected != predecessor:
                        raise HostedGrantUnavailable("Browser ceremony predecessor changed")
                if (current["provider_revision"] != request["predecessor_revision"]
                        or current["presentation_state"] not in {"retired", "expired"}
                        or current["domain_status"] not in {"pending", "awaiting-authentication", "awaiting-confirmation"}
                        or timestamp(json.loads(current["binding_json"])["source_expires_at"]) <= self.store._now()):
                    raise HostedGrantUnavailable("Browser ceremony replacement predecessor changed")
            result = await self._effects.run(self.replacement_client.replace, request, json.loads(row["binding_json"]),
                before_send=guard, journey_id=self._journey(operation_id))
            self.replacement_client.validate_result(request, json.loads(row["binding_json"]), result)
            await asyncio.to_thread(self._adopt_replacement, operation_id, request, result["successor"],
                preparing=True, authorize=authorize)
            return result

    def _replacement_receipt(self, operation):
        intent = json.loads(operation["owner_scope_json"])["ceremony_replacement"]
        parent = intent["parent_operation_id"]
        predecessor = intent["predecessor"]
        with self.store.database.read() as connection:
            saved = self.replacement_reservation(parent, predecessor["predecessor_operation_id"], connection=connection)
            if saved is None:
                return None, False
            if any(saved.get(name) != value for name, value in predecessor.items()):
                raise HostedGrantUnavailable("Replacement receipt changed")
            adopted = connection.execute("SELECT 1 FROM desktop_browser_ceremony_successors "
                "WHERE hosted_operation_id=?", (saved["successor_operation_id"],)).fetchone() is not None
        return saved, adopted

    def _replacement_domain_settled(self, parent):
        with self.store.database.read() as connection:
            row = self.row(parent, connection=connection)
            operation = connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
                (parent,)).fetchone()
            return (row is not None and row["domain_status"] in {"committed", "ready", "revoked"}
                or operation is not None and operation["state"] == "completed")

    async def execute_replacement(self, operation, *, authorize, read_guard, reconcile):
        """One explicit child command; after acceptance, never replay the mutation."""
        from app.onboarding.coordinator import OperationResult
        intent = json.loads(operation["owner_scope_json"])["ceremony_replacement"]
        parent, predecessor = intent["parent_operation_id"], intent["predecessor"]
        def guard(connection=None):
            with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
                authorize(connection=current)
                child = current.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                    (operation["operation_id"],)).fetchone()
                original = current.execute("SELECT journey_id,action FROM desktop_setup_operations WHERE operation_id=?",
                    (parent,)).fetchone()
                if (child is None or child["state"] != "accepted" or child["action"] != "auth.select"
                        or child["owner_scope_json"] != operation["owner_scope_json"]
                        or child["expires_at"] <= self.store._now().timestamp() or original is None
                        or original["journey_id"] != child["journey_id"] or original["action"] != "account.connect"):
                    raise HostedGrantUnavailable("Replacement command changed")
        saved, adopted = await asyncio.to_thread(self._replacement_receipt, operation)
        if adopted:
            return OperationResult("completed")
        if await asyncio.to_thread(self._replacement_domain_settled, parent):
            return OperationResult("refused", "operation_refused")
        if not reconcile and saved is None:
            try:
                await self.replace(parent, predecessor, authorize=guard, allow_expired_parent=True)
            except (HostedGrantUnavailable, BrowserCeremonyRefused):
                # A failed check before a durable reservation cannot have sent.
                # Even a closed refusal after reservation may race the original
                # operation or an earlier committed successor; read, don't replay.
                saved, adopted = await asyncio.to_thread(self._replacement_receipt, operation)
                if not saved:
                    return OperationResult("refused", "operation_refused")
                if adopted:
                    return OperationResult("completed")
                return OperationResult("outcome_unknown", "operation_unconfirmed")
            return OperationResult("completed")
        if saved is None:
            # Restart before reservation: no send was possible. A fresh explicit
            # action can be admitted if the owner still offers it.
            return OperationResult("refused", "operation_unconfirmed")
        await self.reconcile(parent, authorize=lambda: read_guard(),
            adopt_authorize=read_guard)
        _, adopted = await asyncio.to_thread(self._replacement_receipt, operation)
        if adopted:
            return OperationResult("completed")
        if await asyncio.to_thread(self._replacement_domain_settled, parent):
            return OperationResult("refused", "operation_refused")
        return OperationResult("outcome_unknown", "operation_unconfirmed")

    def replacement_pending(self, operation_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            row = self.row(operation_id, connection=connection)
            return (row is not None and self.replacement_client is not None and
                self.replacement_reservation(operation_id, row["hosted_operation_id"], connection=connection) is not None)

    async def reconcile_replacement(self, operation_id, *, authorize, adopt_authorize=None):
        """Read only the durable successor after an interrupted replacement send."""
        return await self.reconcile(operation_id, authorize=authorize, adopt_authorize=adopt_authorize)

    async def _read_replacement(self, operation_id, row, request, *, authorize, adopt_authorize):
        binding = self._replacement_binding(row, request)
        wire = {"profile": PROFILE, "operation_id": request["successor_operation_id"], "binding": binding}
        def guard():
            authorize()
            self._replacement_current(operation_id, request)
        result = await self._effects.run(self.client.read, wire, before_send=guard,
            journey_id=self._journey(operation_id), protected=False)
        self.client.validate_result("read", wire, result)
        if result["status"] == "found":
            await asyncio.to_thread(self._adopt_replacement, operation_id, request, result,
                preparing=False, authorize=adopt_authorize)
        return result

    def _adopt_replacement(self, operation_id, request, result, *, preparing, authorize):
        # One transaction installs the distinct verified successor, retires
        # predecessor-scoped local tasks/commands and invalidates all displays.
        view = result if preparing else result["snapshot"]
        entry = result if preparing else result.get("entry")
        state = "active" if preparing else view["presentation_state"]
        status = "pending" if preparing else view["domain_status"]
        revision = None if preparing else view["revision"]
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            row = self._replacement_current(operation_id, request, connection=connection)
            binding = self._replacement_binding(row, request)
            wire = {"profile": PROFILE, "operation_id": request["successor_operation_id"], "binding": binding}
            self.client.validate_result("prepare" if preparing else "read", wire, result)
            if connection.execute("SELECT 1 FROM desktop_browser_ceremonies WHERE ceremony_id=? UNION ALL "
                    "SELECT 1 FROM desktop_browser_ceremony_successors WHERE ceremony_id=?",
                    (view["ceremony_id"], view["ceremony_id"])).fetchone() is not None:
                raise HostedGrantUnavailable("Browser ceremony replacement reused predecessor")
            if row["domain_status"] in {"committed", "ready", "revoked"}:
                raise HostedGrantUnavailable("Browser ceremony domain outcome takes priority")
            reference = (None if entry is None or state != "active" or timestamp(view["expires_at"]) <= self.store._now()
                else entry["entry_reference"])
            connection.execute("INSERT INTO desktop_browser_ceremony_successors VALUES (?,?,?,?,?,?,?,?)",
                (request["successor_operation_id"], canonical(binding).decode(), view["ceremony_id"],
                 view["expires_at"], reference, state, status, revision))
            connection.execute("DELETE FROM desktop_authentication_projections WHERE operation_id=?", (operation_id,))
            connection.execute("DELETE FROM desktop_decision_projections WHERE operation_id=?", (operation_id,))
            connection.execute("UPDATE desktop_authentication_closures SET state='settled' "
                "WHERE state='pending' AND operation_id IN (SELECT operation_id FROM desktop_authentication_commands "
                "WHERE parent_operation_id=?)", (operation_id,))
            # Include commands admitted but not yet dispatched; their captured
            # local parent is the predecessor and cannot select the successor.
            children = ("SELECT operation_id FROM desktop_setup_operations WHERE operation_id<>? AND ("
                "json_extract(owner_scope_json,'$.authentication_intent.parent_operation_id')=? OR "
                "json_extract(owner_scope_json,'$.desktop_decision.operation_id')=?)")
            scope = (operation_id, operation_id, operation_id)
            connection.execute("UPDATE desktop_browser_tasks SET state='cancelled' WHERE purpose='browser.authentication' "
                "AND state IN ('pending','accepted','outcome_unknown','completed') AND "
                "(operation_id=? OR operation_id IN (" + children + "))", (operation_id, *scope))
            connection.execute("UPDATE desktop_setup_operations SET state='refused',reason='operation_unconfirmed' "
                "WHERE state IN ('accepted','waiting','outcome_unknown') AND operation_id IN (" + children + ")", scope)
            connection.execute("UPDATE desktop_setup_journeys SET revision=revision+1 WHERE journey_id=?",
                (self._journey(operation_id, connection=connection),))
        events.publish()

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

    async def reconcile(self, operation_id, *, authorize, adopt_authorize=None):
        async with self._lock:
            row = self.row(operation_id)
            if row is None:
                raise HostedGrantUnavailable("Browser ceremony outcome is unavailable")
            request = (self.replacement_reservation(operation_id, row["hosted_operation_id"])
                if self.replacement_client is not None else None)
            if request is not None:
                successor = await self._read_replacement(operation_id, row, request, authorize=authorize,
                    adopt_authorize=authorize if adopt_authorize is None else adopt_authorize)
                if successor["status"] == "found":
                    return successor
                # A missing successor is not rollback. Preserve any exact
                # terminal domain result from the original operation instead
                # of preparing a competing source or resending a mutation.
                original = await self._read(row, authorize=authorize)
                if (original["status"] == "found" and original["snapshot"]["domain_status"] in {
                        "committed", "ready", "revoked"}):
                    return original
                return successor
            return await self._read(row, authorize=authorize)

    async def _read(self, row, *, authorize):
        ticket = await self._held.take(row["hosted_operation_id"])
        options = {} if ticket is None else {"prefetched": ticket}
        with timing.phase("ceremony-read", "read-end"):
            result = await self._effects.run(self.client.read, self.request(row), expires_at=row["provider_expires_at"],
                before_send=self.guarded(row, authorize), journey_id=self._journey(row["operation_id"]), protected=False, **options)
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
                before_send=self.guarded(row, authorize, mutation=True), journey_id=self._journey(operation_id))
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
            current = self.row(row["operation_id"], connection=connection)
            if (current is None or current["hosted_operation_id"] != row["hosted_operation_id"]
                    or current["binding_json"] != row["binding_json"]
                    or (current["provider_revision"] is not None and (revision is None or revision < current["provider_revision"]))):
                raise HostedGrantUnavailable("Browser ceremony result changed")
            state = "active" if preparing else view["presentation_state"]
            status = "pending" if preparing else view["domain_status"]
            reference = (None if entry is None or state != "active" or timestamp(view["expires_at"]) <= self.store._now()
                else entry["entry_reference"])
            # Ceremony revision fences workflow writes. Deadline expiry and the
            # independently owned source projection can change at that revision.
            # Reads are serialized and still verify the exact immutable binding.
            root = connection.execute("SELECT hosted_operation_id FROM desktop_browser_ceremonies WHERE operation_id=?",
                (row["operation_id"],)).fetchone()
            if root is not None and root[0] == row["hosted_operation_id"]:
                table = "desktop_browser_ceremonies"
            else:
                table = "desktop_browser_ceremony_successors"
            connection.execute(f"UPDATE {table} SET ceremony_id=?,provider_expires_at=?,entry_reference=?,"
                "presentation_state=?,domain_status=?,provider_revision=? WHERE hosted_operation_id=?",
                (view["ceremony_id"], view["expires_at"], reference, state, status, revision, row["hosted_operation_id"]))
        events.publish()

    def browser_entry(self, operation_id):
        """Read only nonauthorizing launch coordinates for the exact task."""
        # Each helper payload read asks for this, so it opens the store once.
        with self.store.database.read() as connection:
            if self.authentication is not None and not self.authentication.can_open(operation_id, connection=connection):
                return None
            row = self.task_row(operation_id, connection=connection)
            if (row is None or row["entry_reference"] is None or row["presentation_state"] != "active"
                    or timestamp(row["provider_expires_at"]) <= self.store._now()
                    or self.replacement_reservation(row["operation_id"], row["hosted_operation_id"], connection=connection) is not None):
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
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            row = self.row(parent, connection=current)
            command = current.execute("SELECT request_json FROM desktop_authentication_commands WHERE operation_id=?",
                (operation_id,)).fetchone()
            if command is not None and not self.matches_request(row, json.loads(command[0])):
                return None
            return row

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
