"""One durable local setup projection; domain services retain every authority check."""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol
from uuid import uuid4

from app.onboarding import timing
from app.onboarding.admission import AdmittedSession, ScopedAdmissions
from app.onboarding.contract import PROFILE, SNAPSHOT_PROFILE, REASONS, canonical, digest, validate
from app.onboarding.readiness import DesktopAnalysisReadiness
from app.onboarding.browser_tasks import creator_label
from app.persistence.onboarding import require_journey
from app.provisioning.events import events
from app.security.browser_ceremony_wait import CeremonyWaitUnavailable

FACTS = ("browser_access", "installation", "account", "configuration", "enrollment", "pairing", "activation", "analysis")
FACT_STATES = frozenset({"unknown", "missing", "verified", "paused"})
RECOVERY_INTERVAL_SECONDS = 15
WAIT_BACKOFF_CAP_SECONDS = 15
CONFIRMATION_BACKOFF_CAP_SECONDS = 60


class CommandRefused(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Desktop setup command was refused")


@dataclass(frozen=True)
class DomainView:
    facts: dict[str, str]
    # Private exact target/authority generations participate in command fencing.
    # They are not returned as authority, credentials or browser-task payloads.
    scope: dict = field(default_factory=dict)
    reason: str = "none"
    permitted_actions: frozenset[str] | None = None
    analysis_readiness: DesktopAnalysisReadiness = field(default_factory=DesktopAnalysisReadiness)

    def __post_init__(self):
        if (set(self.facts) != set(FACTS) or any(value not in FACT_STATES for value in self.facts.values())
                or self.reason not in REASONS
                or any(value == "paused" and name != "browser_access" for name, value in self.facts.items())):
            raise ValueError("Invalid setup facts")


@dataclass(frozen=True)
class OperationResult:
    state: str
    reason: str = "none"

    def __post_init__(self):
        if self.state not in {"waiting", "outcome_unknown", "completed", "refused"} or self.reason not in REASONS:
            raise ValueError("Invalid setup operation result")


@dataclass(frozen=True)
class AcceptedContext:
    journey_id: str
    scope: dict


class SetupOwner(Protocol):
    def read(self, journey_id: str | None, *, actor=None, connection=None) -> DomainView:
        """Read local facts only. Do not prepare, resume or redeem anything."""
        ...

    def admit(self, connection, journey_id: str | None, *, action: str, scope: dict, actor=None) -> AcceptedContext:
        """Open or retain an exact domain context during explicit command admission."""
        ...

    async def execute(self, operation: dict, *, reconcile: bool, actor=None) -> OperationResult:
        """Reconcile only the original accepted operation, never replace its scope."""
        ...

    def observe(self, operation: dict) -> OperationResult | None:
        """Read the original operation's local outcome without protected work."""
        ...


class ProbingSetupOwner(Protocol):
    """Optional owner hooks for work whose outcome another party may settle soon."""

    def probe_interval(self, operation: dict) -> float | None:
        """Seconds between cheap outcome reads, or None for the ordinary recovery cadence."""
        ...

    async def probe(self, operation: dict) -> bool:
        """Read without writing. True means the full reconciliation may now settle more."""
        ...

    def wait(self, operation: dict) -> AsyncIterator[tuple]:
        """Held outcome hints for the same work. A hint only starts the full reconciliation."""
        ...


@dataclass(frozen=True)
class SetupPresentation:
    """The local presentation coordinates, not an HTTP session credential."""
    generation: str
    journey_id: str | None
    stage: str
    expires_at: float


class DesktopOnboardingCoordinator:
    def __init__(self, admissions: ScopedAdmissions, owner: SetupOwner):
        self.admissions, self.owner = admissions, owner
        self.store = admissions.authentication
        self._workers: dict[str, asyncio.Task] = {}
        self._watcher: asyncio.Task | None = None
        self._closed = False
        self._retry_at: dict[str, float] = {}
        self._retry_failures: dict[str, int] = {}
        self._accepting: set[asyncio.Task] = set()
        self._probes: dict[str, asyncio.Task] = {}
        self._probe_at: dict[str, float] = {}
        self._waits: dict[str, asyncio.Task] = {}
        self._held: set[str] = set()
        self._wake: asyncio.Event | None = None
        self._dirty: dict[str, bool] = {}
        # The wait frame behind the next hinted run, and the frame outcome a hinted read already closed a task on.
        self._hint_frames: dict[str, tuple] = {}
        self._closed_on: dict[str, str | None] = {}
        self._prefetches: dict[str, asyncio.Task] = {}
        self._prefetch_wake: dict[str, asyncio.Event] = {}
        self._confirmation_at: dict[str, float] = {}
        self._wait_marks: dict[tuple[str | None, str], float | None] = {}
        # Presentation-only notice of a freshly accepted command; it never changes the operation.
        self.accepted: Callable[[dict], None] | None = None

    def _current(self, connection, session):
        row, parent = self.admissions._require(connection, session.identifier, role="desktop", generation=session.generation)
        return self.admissions._session(session.identifier, row, parent)

    def _selected(self, connection, session):
        if session.journey_id is not None:
            return session.journey_id
        rows = connection.execute("SELECT journey_id FROM desktop_setup_journeys ORDER BY created_at DESC LIMIT 2").fetchall()
        if len(rows) > 1:
            raise CommandRefused("setup_selection_required")
        return None if not rows else rows[0]["journey_id"]

    def read(self, session: AdmittedSession, *, actor=None) -> dict:
        """No journal writes, owner work, task creation or network calls."""
        # The owner's reads for this snapshot share its connection.
        with self.store.database.shared_reads(), self.store.database.read() as connection:
            return self._snapshot(connection, self._current(connection, session), actor=actor)

    def _snapshot(self, connection, session, *, actor=None, view=None):
        journey_id = self._selected(connection, session)
        state = None if journey_id is None else connection.execute(
            "SELECT revision FROM desktop_setup_journeys WHERE journey_id=?", (journey_id,)).fetchone()
        revision = 0 if state is None else state["revision"]
        if view is None:
            view = self.owner.read(journey_id, actor=actor, connection=connection)
        operation_rows = [] if journey_id is None else connection.execute(
            "SELECT * FROM desktop_setup_operations WHERE journey_id=? AND state IN ('accepted','waiting','outcome_unknown') ORDER BY created_at, operation_id",
            (journey_id,)).fetchall()
        pending = [self._public(row) for row in operation_rows]
        facts = dict(view.facts)
        local_ready = all(facts[name] == "verified" for name in ("installation", "account", "configuration", "enrollment", "pairing"))
        if local_ready:
            task = "complete"
        elif facts["installation"] == "verified" and facts["account"] == "verified":
            task = "finish-setup"
        elif any(item["action"] == "account.connect" for item in pending):
            # A replacement presentation can lose its browser observation while
            # the accepted account operation still owns the current task.
            task = "account-connection"
        elif any(item["action"] in {"setup.finalize", "browser.pair"} for item in pending):
            task = "finish-setup"
        elif facts["browser_access"] not in {"verified", "paused"} and facts["configuration"] != "verified":
            task = "browser-access"
        elif facts["installation"] != "verified" or facts["account"] != "verified":
            task = "account-connection"
        else:
            task = "finish-setup"
        actions = []
        reason = view.reason
        if task == "finish-setup" and facts["configuration"] != "verified" and self._automatic_finalization_enabled():
            refusal = self._finalization_receipt(connection, journey_id, view)
            if refusal is not None and refusal["state"] == "refused" and reason == "none":
                reason = refusal["reason"]
        selected_decision = view.scope.get("desktop_decision")
        authentication = view.scope.get("desktop_authentication")
        if authentication is not None and (authentication["state"] in {
                "selection-required", "reauthentication-required", "selected", "expired", "unavailable"}
                or authentication["actions"]["open_browser"] or any(
                    row["action"] in {"auth.select", "auth.switch", "auth.cancel"}
                    and json.loads(row["owner_scope_json"]).get("authentication_intent", {}).get("parent_operation_id")
                        == authentication["operation_id"] for row in operation_rows)):
            selected_decision = None
        decision = None if selected_decision is None else {
            "operation_id": selected_decision["operation_id"],
            **selected_decision["projection"]["decision"],
        }
        if reason == "none":
            if task == "browser-access":
                actions.append("browser.access")
            elif task == "account-connection" and facts["browser_access"] in {"verified", "paused"}:
                actions.append("account.connect")
            elif task == "finish-setup" and facts["installation"] == facts["account"] == "verified":
                if facts["configuration"] != "verified":
                    actions.append("setup.finalize")
                elif facts["enrollment"] == "verified" and facts["pairing"] != "verified":
                    actions.append("browser.pair")
            if local_ready and facts["activation"] != "verified":
                actions.append("activation.review")
            if session.stage == "runtime" and facts["configuration"] == facts["enrollment"] == "verified":
                actions.append("browser.access")
            if decision is not None:
                actions.append("account.confirm" if decision["action"] == "connect-account" else "activation.confirm")
            if authentication is not None and authentication["operation_id"] is not None:
                for action, permitted in (("auth.select", "select"), ("auth.cancel", "cancel"), ("auth.switch", "switch_account")):
                    if authentication["actions"][permitted]:
                        actions.append(action)
        actions = [name for name in actions if all(item["action"] != name or (
            name == "auth.select" and authentication is not None
            and authentication["state"] == "reauthentication-required" and authentication["actions"]["select"]
            and item["operation_id"] == authentication["browser_operation_id"]) for item in pending)]
        if view.permitted_actions is not None:
            actions = [name for name in actions if name in view.permitted_actions]
        return validate({"profile": SNAPSHOT_PROFILE, "kind": "snapshot", "journey_id": journey_id,
            "generation": session.generation, "revision": revision, "expires_at": session.expires_at,
            "scope_digest": digest({"journey_id": journey_id, "generation": session.generation,
                "revision": revision, "stage": session.stage, "facts": facts, "scope": view.scope, "reason": reason,
                "permitted_actions": None if view.permitted_actions is None else sorted(view.permitted_actions)}),
            "next_task": task, "facts": facts, "local_ready": local_ready,
            "full_ready": local_ready and facts["activation"] == "verified" and facts["analysis"] == "verified",
            "pending_operations": pending, "allowed_actions": actions, "reason": reason,
            "decision": decision, "decision_status": view.scope.get("desktop_decision_status"),
            "continuation_available": view.scope.get("source_recovery") is not None,
            "creator_label": creator_label(view.scope.get("browser"), view.scope.get("creator_account_id")),
            "authentication": authentication, "analysis_readiness": view.analysis_readiness.projection()}, kind="snapshot")

    @staticmethod
    def _public(row):
        return {name: row[name] for name in ("operation_id", "action", "state", "reason")}

    def receipt(self, session, operation_id):
        require_journey(operation_id)
        with self.store.database.read() as connection:
            current = self._current(connection, session)
            row = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row["journey_id"] != self._selected(connection, current):
                raise CommandRefused("operation_unavailable")
            return self._public(row)

    @timing.measured("command", "accept")
    def _accept(self, session, command, *, actor=None):
        request_digest = digest(command)
        with self.store.database.transaction() as connection:
            current = self._current(connection, session)
            previous = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                                          (command["operation_id"],)).fetchone()
            if previous is not None:
                if (previous["request_digest"] != request_digest
                        or previous["journey_id"] != self._selected(connection, current)):
                    raise CommandRefused("operation_conflict")
                return dict(previous), False
            view = self.owner.read(self._selected(connection, current), actor=actor, connection=connection)
            snapshot = self._snapshot(connection, current, actor=actor, view=view)
            row = self._record_command(connection, current, command, view, snapshot, actor=actor)
            self._current(connection, session)
            return row, True

    def _record_command(self, connection, current, command, view, snapshot, *, actor=None):
        request_digest = digest(command)
        expected = command["expected"]
        if (command["journey_id"] != snapshot["journey_id"]
                or any(expected[name] != snapshot[name] for name in ("generation", "revision", "scope_digest"))):
            raise CommandRefused("setup_changed")
        if command["action"] not in snapshot["allowed_actions"]:
            raise CommandRefused("action_unavailable")
        scope = dict(view.scope)
        action = command["action"]
        authentication = snapshot["authentication"]
        if action.startswith("auth.") or action == "account.connect" and "provider" in command:
            from app.persistence.auth import _new_uuid7
            provider = command.get("provider")
            if authentication is None or provider is not None and not authentication["choices"].get(provider, False):
                raise CommandRefused("action_unavailable")
            attempt = authentication["authentication_attempt"]
            if action == "auth.cancel" and attempt is None:
                raise CommandRefused("action_unavailable")
            scope["authentication_intent"] = {
                "parent_operation_id": command["operation_id"] if action == "account.connect" else authentication["operation_id"],
                "verb": "select" if action == "account.connect" else action.removeprefix("auth."),
                "authentication_attempt_id": attempt["authentication_attempt_id"] if action == "auth.cancel" else _new_uuid7(self.store._now()),
                "expected_previous_attempt_id": None if attempt is None else attempt["authentication_attempt_id"],
                "provider": provider,
            }
        accepted = self.owner.admit(connection, snapshot["journey_id"], action=action, scope=scope, actor=actor)
        journey = accepted.journey_id
        if snapshot["journey_id"] is not None and journey != snapshot["journey_id"]:
            raise CommandRefused("setup_changed")
        require_journey(journey)
        now = self.admissions._now()
        if now >= current.expires_at:
            raise CommandRefused("setup_changed")
        # Retain unresolved work independently of presentation expiry. Bound
        # new commands without preventing reads or native reattachment.
        connection.execute("DELETE FROM desktop_setup_operations WHERE state IN ('completed','refused') AND expires_at<=? "
            "AND NOT (action='setup.finalize' AND state='refused') AND NOT EXISTS (SELECT 1 "
            "FROM desktop_browser_ceremony_replacements r WHERE r.parent_operation_id=desktop_setup_operations.operation_id "
            "AND julianday(r.source_expires_at)>julianday(?, 'unixepoch'))", (now, now))
        if connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] >= 128:
            raise CommandRefused("operation_limit_reached")
        connection.execute("INSERT OR IGNORE INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)", (journey, now))
        connection.execute("UPDATE desktop_setup_journeys SET entry_source=(SELECT entry_source FROM desktop_presentations "
            "WHERE generation=?) WHERE journey_id=?", (current.generation, journey))
        # The journal cannot silently jump from one selected workspace to another.
        connection.execute("UPDATE desktop_presentations SET journey_id=? WHERE generation=?",
                           (journey, current.generation))
        if (action == "auth.select" and authentication["state"] == "reauthentication-required"
                and authentication["actions"]["select"]):
            # The replacement click retires only the ended selection's local receipt.
            connection.execute("UPDATE desktop_setup_operations SET state='refused',reason='operation_unconfirmed' "
                "WHERE operation_id=? AND journey_id=? AND action='auth.select' "
                "AND state IN ('accepted','waiting','outcome_unknown')",
                (authentication["browser_operation_id"], journey))
        connection.execute("INSERT INTO desktop_setup_operations VALUES (?,?,?,?,?,?,?,?,'accepted','none',?,?)",
            (command["operation_id"], journey, command["action"], request_digest, current.generation,
             expected["scope_digest"], expected["revision"], canonical(accepted.scope).decode(), now, current.expires_at))
        connection.execute("UPDATE desktop_setup_journeys SET revision=revision+1 WHERE journey_id=?", (journey,))
        row = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (command["operation_id"],)).fetchone()
        return dict(row)

    def _automatic_finalization_enabled(self):
        return self.admissions.stage == "setup" and getattr(self.owner, "automatic_finalization", False)

    @staticmethod
    def _finalization_receipt(connection, journey_id, view):
        if journey_id is None:
            return None
        # The immutable installation/account tuple is independent of presentation
        # expiry, browser observation revisions and status-only invalidations.
        keys = ("source_journey_id", "journey_kind", "installation_id", "association_request_id", "creator_account_id", "key")
        rows = connection.execute("SELECT * FROM desktop_setup_operations WHERE journey_id=? AND action='setup.finalize' "
            "ORDER BY created_at DESC,operation_id", (journey_id,)).fetchall()
        for row in rows:
            scope = json.loads(row["owner_scope_json"])
            if all(scope.get(key) == view.scope.get(key) for key in keys):
                return row
        return None

    def _finalization_candidate(self, connection):
        # Reuse the live, consumed native presentation's exact coordinates.
        # No request credential is manufactured and no renderer read starts work.
        now = self.admissions._now()
        presentations = connection.execute(
            "SELECT p.* FROM desktop_presentations p JOIN desktop_admission_sessions s "
            "ON s.reference_id=p.generation AND s.generation=p.generation "
            "WHERE p.stage='setup' AND p.state='active' AND p.expires_at>? "
            "AND s.role='desktop' AND s.stage='setup' AND s.expires_at>? LIMIT 2", (now, now)).fetchall()
        if len(presentations) != 1:
            return None
        row = presentations[0]
        current = SetupPresentation(row["generation"], row["journey_id"], row["stage"], row["expires_at"])
        journey_id = self._selected(connection, current)
        if journey_id is None:
            return None
        view = self.owner.read(journey_id, connection=connection)
        snapshot = self._snapshot(connection, current, view=view)
        if ("setup.finalize" not in snapshot["allowed_actions"] or snapshot["pending_operations"]
                or snapshot["decision"] is not None or view.scope.get("source_recovery") is not None
                or self._finalization_receipt(connection, journey_id, view) is not None
                or not all(view.scope.get(name) for name in ("source_journey_id", "journey_kind",
                    "installation_id", "association_request_id", "creator_account_id", "key"))):
            return None
        # An outstanding confirmation or sign-in cannot be accepted by local work.
        status = snapshot["decision_status"]
        authentication = snapshot["authentication"]
        if (status is not None and status["state"] != "accepted"
                or authentication is not None and authentication["state"] != "admitted"):
            return None
        return current, view, snapshot

    def _admit_automatic_finalization(self):
        if self._closed or not self._automatic_finalization_enabled():
            return None
        try:
            # Most wakeups have no work. Do not open a write transaction for them.
            with self.store.database.read() as connection:
                candidate = self._finalization_candidate(connection)
            if candidate is None:
                return None
            expected = candidate[2]["scope_digest"]
            with self.store.database.transaction() as connection:
                candidate = self._finalization_candidate(connection)
                if candidate is None or candidate[2]["scope_digest"] != expected:
                    return None
                current, view, snapshot = candidate
                command = validate({"profile": PROFILE, "kind": "command", "action": "setup.finalize",
                    "operation_id": str(uuid4()), "journey_id": snapshot["journey_id"],
                    "expected": {name: snapshot[name] for name in ("generation", "revision", "scope_digest")}},
                    kind="command")
                return self._record_command(connection, current, command, view, snapshot)
        except Exception:
            # Admission is refused without creating an operation. The next owner
            # invalidation can re-evaluate changed authority, not replay a command.
            return None

    def _continue_finalization(self):
        operation = self._admit_automatic_finalization()
        if operation is not None:
            events.publish()
            self._start(operation, reconcile=False, actor=None)

    async def command(self, session, command, *, actor=None):
        validate(command, kind="command")
        if self._closed:
            raise CommandRefused("setup_unavailable")
        async def accept():
            # The accept is durable before this returns. Its transaction runs off the event loop.
            operation, fresh = await asyncio.to_thread(self._accept, session, command, actor=actor)
            if fresh:
                events.publish()
                if self.accepted is not None:
                    try:
                        self.accepted(operation)
                    except Exception:
                        pass
                if not self._closed:
                    self._start(operation, reconcile=False, actor=actor)
            return operation
        # HTTP cancellation cannot cancel accepted domain work. A duplicate only
        # observes its receipt; it never starts a second worker.
        task = asyncio.create_task(accept())
        self._accepting.add(task)
        task.add_done_callback(self._accepting.discard)
        operation = await asyncio.shield(task)
        return self._public(operation)

    async def reconcile(self, session, operation_id, *, actor=None):
        if self._closed:
            raise CommandRefused("setup_unavailable")
        self.receipt(session, operation_id)
        with self.store.database.read() as connection:
            operation = dict(connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone())
        if operation["state"] in {"accepted", "waiting", "outcome_unknown"}:
            self._start(operation, reconcile=True, actor=actor)
        return self._public(operation)

    def _finish_worker(self, operation_id, worker, result):
        if self._workers.get(operation_id) is not worker:
            return
        self._workers.pop(operation_id, None)
        failures = self._retry_failures.get(operation_id, 0) if result is not None and result.state == "outcome_unknown" else 0
        self._retry_at[operation_id] = asyncio.get_running_loop().time() + min(
            CONFIRMATION_BACKOFF_CAP_SECONDS, RECOVERY_INTERVAL_SECONDS * 2 ** min(failures, 2))
        if result is not None and result.state == "outcome_unknown":
            self._retry_failures[operation_id] = min(failures + 1, 2)
        else:
            self._retry_failures.pop(operation_id, None)
        if operation_id in self._dirty:
            dirty_hint = self._dirty.pop(operation_id)
            if not self._closed:
                current = next((row for row in self._pending() if row["operation_id"] == operation_id), None)
                if current is not None:
                    self._start(current, reconcile=True, actor=None, hint=dirty_hint)
        events.publish()

    def _start(self, operation, *, reconcile, actor, hint=False):
        operation_id = operation["operation_id"]
        running = self._workers.get(operation_id)
        if running is not None and not running.done():
            if reconcile or hint:
                self._dirty[operation_id] = self._dirty.get(operation_id, True) and bool(hint)
            return
        task = asyncio.create_task(self._run(operation, reconcile=reconcile, actor=actor, hint=hint, queued=time.monotonic()))
        self._workers[operation_id] = task

        def finished(worker):
            try:
                result = worker.result()
            except (Exception, asyncio.CancelledError):
                result = OperationResult("outcome_unknown", "operation_unconfirmed")
            self._finish_worker(operation_id, worker, result)

        task.add_done_callback(finished)

    async def _run(self, operation, *, reconcile, actor, hint=False, queued=None):
        timing.observe("worker", "entry", "ok", 0 if queued is None else (time.monotonic() - queued) * 1000)
        operation_id = operation["operation_id"]
        frame = self._hint_frames.pop(operation_id, None) if hint else None
        result = None
        outcome = OperationResult("outcome_unknown", "operation_unconfirmed")
        try:
            try:
                if hint and getattr(self.owner, "reconcile_hint", None) is not None:
                    if (frame is not None and operation_id in self._closed_on and self._closed_on[operation_id] == frame[2]
                            and self._probe_interval(operation) is None):
                        # A hinted read already closed this task on the same outcome. Nothing is left to read.
                        outcome = None
                        return None
                    closed = await self.owner.reconcile_hint(operation)
                    if closed is False:
                        self._prefetch(operation)
                        outcome = None
                        return None
                    if closed is True:
                        self._closed_on[operation_id] = None if frame is None else frame[2]
                        if operation["action"] in {"auth.select", "auth.switch"}:
                            # The read that closed the task admitted this child's own attempt, which completes it.
                            result = OperationResult("completed")
                    # A completed task is published before the remaining owner reads.
                if result is None:
                    result = await self.owner.execute(operation, reconcile=reconcile, actor=actor)
            except asyncio.CancelledError:
                self._settle(operation, OperationResult("outcome_unknown", "operation_unconfirmed"))
                raise
            except Exception:
                # No transport exception proves that a protected operation rolled back.
                result = OperationResult("outcome_unknown", "operation_unconfirmed")
            outcome = result
            self._settle(operation, result)
            if result.state != "outcome_unknown":
                await self._refresh_authentication_parent(operation)
            return result
        finally:
            self._finish_worker(operation_id, asyncio.current_task(), outcome)

    def _authentication_parent(self, operation):
        if self._closed or operation["action"] not in {"auth.select", "auth.cancel", "auth.switch"}:
            return None
        selected = json.loads(operation["owner_scope_json"])
        intent = selected.get("authentication_intent")
        if not isinstance(intent, dict):
            return None
        parent_id = intent.get("parent_operation_id")
        if parent_id is None or parent_id == operation["operation_id"]:
            return None
        with self.store.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_setup_operations WHERE operation_id=? AND journey_id=? "
                "AND action IN ('account.connect','activation.review') "
                "AND state IN ('accepted','waiting','outcome_unknown')",
                (parent_id, operation["journey_id"])).fetchone()
        if row is None:
            return None
        parent = dict(row)
        recover = getattr(self.owner, "automatic_recovery", None)
        try:
            return parent if recover is not None and recover(parent) else None
        except Exception:
            return None

    async def _refresh_authentication_parent(self, operation):
        parent = self._authentication_parent(operation)
        if parent is None:
            return
        running = self._workers.get(parent["operation_id"])
        if running is not None and not running.done():
            # Its read may have started before the child changed admission. Keep
            # one fresh read after that worker, including during drain.
            await asyncio.shield(running)
            parent = self._authentication_parent(operation)
        if parent is not None:
            self._start(parent, reconcile=True, actor=None)

    def _settle(self, operation, result):
        with self.store.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE desktop_setup_operations SET state=?,reason=? WHERE operation_id=? AND state IN ('accepted','waiting','outcome_unknown') AND (state<>? OR reason<>?)",
                (result.state, result.reason, operation["operation_id"], result.state, result.reason)).rowcount
            if changed:
                connection.execute("UPDATE desktop_setup_journeys SET revision=revision+1 WHERE journey_id=?", (operation["journey_id"],))
        if changed:
            timing.mark(operation["operation_id"], "reconciled")
            events.publish()
            settled = getattr(self.owner, "settled", None)
            if settled is not None:
                settled(operation, result)

    def _pending(self):
        with self.store.database.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM desktop_setup_operations "
                "WHERE state IN ('accepted','waiting','outcome_unknown') ORDER BY created_at,operation_id").fetchall()]

    def start(self):
        if self._closed or self._watcher is not None:
            return
        # Process startup resumes only already accepted commands. Rendering a
        # page is never the trigger for source preparation or result recovery.
        for operation in self._pending():
            self._start(operation, reconcile=True, actor=None)
        self._watcher = asyncio.create_task(self._watch())

    def _pass_facts(self):
        """The store reads of one watch pass: each pending operation with its recovery, probe and observation."""
        facts = []
        for operation in self._pending():
            recover = getattr(self.owner, "automatic_recovery", None)
            try:
                recoverable = bool(recover is not None and recover(operation))
            except Exception:
                recoverable = False
            waiting = getattr(self.owner, "waiting_for_sign_in", None)
            if recoverable and waiting is not None:
                try:
                    recoverable = not waiting(operation)
                except Exception:
                    pass
            interval = self._probe_interval(operation) if recoverable else None
            try:
                observed = self.owner.observe(operation)
            except Exception:
                # One unavailable authority must not stop reconciliation
                # for every later event. No observation proves rollback.
                observed = OperationResult("outcome_unknown", "operation_unconfirmed")
            facts.append((operation, recoverable, interval, observed, self._confirmation_deadline(operation)))
        return facts

    async def _watch(self):
        listener = events.subscribe()
        signal = self._wake = listener[1]
        try:
            while not self._closed:
                signal.clear()
                started = time.monotonic()
                # Finalization admission and pass reads run off the event loop. A commit during the read wakes the next pass.
                finalized = await asyncio.to_thread(self._admit_automatic_finalization)
                if finalized is not None and not self._closed:
                    events.publish()
                    self._start(finalized, reconcile=False, actor=None)
                facts = await asyncio.to_thread(self._pass_facts)
                if self._closed:
                    break
                pending_ids = {operation["operation_id"] for operation, *_ in facts}
                self._retry_at = {key: value for key, value in self._retry_at.items() if key in pending_ids}
                self._retry_failures = {key: value for key, value in self._retry_failures.items() if key in pending_ids}
                self._probe_at = {key: value for key, value in self._probe_at.items() if key in pending_ids}
                self._hint_frames = {key: value for key, value in self._hint_frames.items() if key in pending_ids}
                self._closed_on = {key: value for key, value in self._closed_on.items() if key in pending_ids}
                self._confirmation_at = {key: value for key, value in self._confirmation_at.items() if key in pending_ids}
                self._wait_marks = {key: value for key, value in self._wait_marks.items() if key[0] in pending_ids}
                for operation_id in [key for key in self._waits if key not in pending_ids]:
                    self._waits.pop(operation_id).cancel()
                retry_delays, remaining, recovery = [], [], False
                for operation, recoverable, interval, observed, confirmation in facts:
                    operation_id = operation["operation_id"]
                    now = self.admissions._now()
                    expired = self._confirmation_at.get(operation_id, float("inf")) <= now
                    if expired:
                        self._confirmation_at.pop(operation_id, None)
                        timing.observe("worker", "expiry", "ok", 0)
                        self._start(operation, reconcile=True, actor=None)
                    if confirmation is None:
                        self._confirmation_at.pop(operation_id, None)
                    if confirmation is not None:
                        self._confirmation_at[operation_id] = confirmation
                        prefetch = self._prefetches.get(operation_id)
                        if prefetch is not None:
                            prefetch.cancel()
                        retry_delays.append(max(0, confirmation - now))
                    if recoverable and (confirmation is None or operation_id not in self._waits):
                        recovery = True
                    if recoverable and interval is not None:
                        self._hold_wait(operation)
                    if interval is None:
                        wait = self._waits.pop(operation_id, None)
                        if wait is not None:
                            wait.cancel()
                        prefetch = self._prefetches.get(operation_id)
                        if prefetch is not None:
                            prefetch.cancel()
                    if observed is not None:
                        self._settle(operation, observed)
                        continue
                    remaining.append(operation)
                    if operation["operation_id"] not in self._workers and recoverable:
                        # Only domain-approved recovery of accepted work. No
                        # new command, browser window or presentation is created.
                        clock = asyncio.get_running_loop().time()
                        if interval is not None and operation_id in self._held:
                            # The held wait replaces probes and retries.
                            continue
                        if confirmation is not None and operation_id in self._waits:
                            # The wait owns recovery and backoff during confirmation.
                            continue
                        if interval is not None and confirmation is None:
                            # A cheap read replaces the slow full retry while
                            # its sign-in is open. A change starts the full read.
                            if operation["operation_id"] not in self._probes:
                                due = self._probe_at.setdefault(operation["operation_id"], clock + interval)
                                if due <= clock:
                                    self._start_probe(operation, interval)
                                else:
                                    retry_delays.append(due - clock)
                            continue
                        due = self._retry_at.setdefault(operation["operation_id"], clock + RECOVERY_INTERVAL_SECONDS)
                        if due <= clock:
                            timing.observe("worker", "retry", "ok", 0)
                            self._start(operation, reconcile=True, actor=None)
                        else:
                            retry_delays.append(due - clock)
                elapsed = (time.monotonic() - started) * 1000
                if elapsed >= timing.SLOW_MS:
                    timing.observe("snapshot", "watch", "ok", elapsed)
                now = self.admissions._now()
                deadlines = [operation["expires_at"] - now for operation in remaining if operation["expires_at"] > now]
                deadlines.extend(retry_delays)
                if facts and recovery:
                    # Pairing and source owners can expire before the enclosing
                    # desktop command. Observe their result without requiring a
                    # renderer poll or replaying a protected command.
                    deadlines.append(RECOVERY_INTERVAL_SECONDS)
                if deadlines:
                    try:
                        await asyncio.wait_for(signal.wait(), min(deadlines))
                    except TimeoutError:
                        pass
                else:
                    await signal.wait()
        finally:
            self._wake = None
            events.unsubscribe(listener)
    def _probe_interval(self, operation):
        interval = getattr(self.owner, "probe_interval", None)
        if interval is None or getattr(self.owner, "probe", None) is None:
            self._wait_mark(operation, "eligibility", None)
            return None
        try:
            value = interval(operation)
        except Exception:
            timing.observe("wait", "eligibility-error", "error", 0)
            return None
        result = value if isinstance(value, (int, float)) and value > 0 else None
        self._wait_mark(operation, "eligibility", result)
        return result

    def _confirmation_deadline(self, operation):
        deadline = getattr(self.owner, "confirmation_deadline", None)
        try:
            value = None if deadline is None else deadline(operation)
        except Exception:
            timing.observe("wait", "eligibility-error", "error", 0)
            return None
        result = value if type(value) in {int, float} and value > self.admissions._now() else None
        self._wait_mark(operation, "confirmation", result)
        return result

    def _wait_mark(self, operation, kind, value):
        key = operation.get("operation_id"), kind
        if key in self._wait_marks and self._wait_marks[key] == value:
            return
        if len(self._wait_marks) >= 256:
            self._wait_marks.pop(next(iter(self._wait_marks)))
        self._wait_marks[key] = value
        phase = (("eligible" if value is not None else "ineligible") if kind == "eligibility"
            else ("confirmation-live" if value is not None else "confirmation-absent"))
        timing.observe("wait", phase, "ok", 0)

    def _start_probe(self, operation, interval):
        operation_id = operation["operation_id"]

        async def run():
            try:
                changed = await self.owner.probe(operation)
                delay = interval
            except asyncio.CancelledError:
                raise
            except Exception:
                # An unavailable read proves nothing. Fall back to the ordinary
                # full reconciliation and its slower cadence.
                changed, delay = True, RECOVERY_INTERVAL_SECONDS
            self._probe_at[operation_id] = asyncio.get_running_loop().time() + delay
            if changed and not self._closed:
                timing.observe("worker", "probe", "ok", 0)
                self._start(operation, reconcile=True, actor=None)

        task = asyncio.create_task(run())
        self._probes[operation_id] = task

        def finished(probe):
            if self._probes.get(operation_id) is probe:
                self._probes.pop(operation_id, None)
            if self._wake is not None:
                self._wake.set()

        task.add_done_callback(finished)

    def _hold_wait(self, operation):
        operation_id = operation["operation_id"]
        if self._closed or operation_id in self._waits or getattr(self.owner, "wait", None) is None:
            return
        timing.observe("wait", "entry", "ok", 0)
        task = asyncio.create_task(self._run_wait(operation))
        self._waits[operation_id] = task

        def finished(wait):
            if self._waits.get(operation_id) is wait:
                self._waits.pop(operation_id, None)
            self._held.discard(operation_id)
            if self._wake is not None:
                self._wake.set()

        task.add_done_callback(finished)

    def _prefetch(self, operation):
        """Keep one fresh read challenge while this sign-in can still be open."""
        operation_id = operation["operation_id"]
        if self._closed or getattr(self.owner, "prefetch", None) is None:
            return
        if operation_id in self._prefetches:
            self._prefetch_wake[operation_id].set()
            return
        wake = self._prefetch_wake[operation_id] = asyncio.Event()

        async def run():
            try:
                while not self._closed:
                    if self._confirmation_deadline(operation) is not None or self._probe_interval(operation) is None:
                        return
                    wake.clear()
                    delay = await self.owner.prefetch(operation)
                    if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 < delay < 3600
                            or self._probe_interval(operation) is None):
                        return
                    try:
                        await asyncio.wait_for(wake.wait(), delay)
                    except TimeoutError:
                        pass
                    if self._probe_interval(operation) is None:
                        return
            except Exception:
                pass
            finally:
                self._prefetches.pop(operation_id, None)
                self._prefetch_wake.pop(operation_id, None)

        self._prefetches[operation_id] = asyncio.create_task(run())

    async def _run_wait(self, operation):
        operation_id, attempt, last, read = operation["operation_id"], 0, None, False
        while not self._closed and self._probe_interval(operation) is not None:
            delay, framed = None, False
            timing.observe("wait", "reopen", "ok", 0)
            try:
                async for hint in self.owner.wait(operation):
                    timing.observe("wait", "frame", "ok", 0)
                    if not framed:
                        self._prefetch(operation)
                    framed, read = True, False
                    if operation_id not in self._held:
                        self._held.add(operation_id)
                        self._probe_at.pop(operation_id, None)
                        if self._confirmation_deadline(operation) is None:
                            attempt = 0
                    if hint != last:
                        last = hint
                        timing.mark(operation_id, "frame")
                        self._reconcile_soon(operation, frame=hint)
            except asyncio.CancelledError:
                raise
            except CeremonyWaitUnavailable as error:
                timing.observe("wait", "unavailable", "error", 0)
                delay = error.retry_after
            except Exception:
                # A lost wait proves nothing. The probe resumes at once.
                timing.observe("wait", "failed", "error", 0)
                pass
            finally:
                timing.observe("wait", "released", "ok", 0)
                self._held.discard(operation_id)
                if self._wake is not None:
                    self._wake.set()
            if self._closed or self._probe_interval(operation) is None:
                return
            confirmation = self._confirmation_deadline(operation) is not None
            if confirmation or not framed and not read:
                # A failed confirmation wait gets one recovery read.
                read = True
                timing.observe("wait", "fallback", "ok", 0)
                self._reconcile_soon(operation)
            if confirmation:
                delay = max(delay or 0, min(CONFIRMATION_BACKOFF_CAP_SECONDS,
                    RECOVERY_INTERVAL_SECONDS * 2 ** min(attempt, 2)))
            elif delay is None:
                delay = min(WAIT_BACKOFF_CAP_SECONDS, 0.25 * 2 ** attempt) * random.uniform(0.5, 1.0)
            attempt += 1
            with timing.phase("wait", "backoff"):
                await asyncio.sleep(delay)

    def _reconcile_soon(self, operation, frame=None):
        """Reconcile now, or once more after a running reconciliation whose read may predate the hint."""
        timing.observe("worker", "wait-fallback" if frame is None else "wait-frame", "ok", 0)
        if frame is None:
            self._hint_frames.pop(operation["operation_id"], None)
        else:
            self._hint_frames[operation["operation_id"]] = frame
        self._start(operation, reconcile=True, actor=None, hint=True)

    async def drain(self):
        while self._workers:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._workers.values())), return_exceptions=True)

    async def stop(self):
        self.begin_shutdown()
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        probes = tuple(self._probes.values()) + tuple(self._waits.values()) + tuple(self._prefetches.values())
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
        await self.drain()

    def begin_shutdown(self):
        """Close presentation admission before transport waits for responses.

        Accepted owner work remains tracked until stop() drains it. This flag
        only refuses new commands/reconciliation and ends presentation streams.
        """
        self._closed = True
        events.publish()
