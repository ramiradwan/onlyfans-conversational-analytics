"""One durable local setup projection; domain services retain every authority check."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Protocol

from app.onboarding.admission import AdmittedSession, ScopedAdmissions
from app.onboarding.contract import SNAPSHOT_PROFILE, REASONS, canonical, digest, validate
from app.onboarding.readiness import DesktopAnalysisReadiness
from app.onboarding.browser_tasks import creator_label
from app.persistence.onboarding import require_journey
from app.provisioning.events import events

FACTS = ("browser_access", "installation", "account", "configuration", "enrollment", "pairing", "activation", "analysis")
FACT_STATES = frozenset({"unknown", "missing", "verified", "paused"})
RECOVERY_INTERVAL_SECONDS = 15


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


class DesktopOnboardingCoordinator:
    def __init__(self, admissions: ScopedAdmissions, owner: SetupOwner):
        self.admissions, self.owner = admissions, owner
        self.store = admissions.authentication
        self._workers: dict[str, asyncio.Task] = {}
        self._watcher: asyncio.Task | None = None
        self._closed = False
        self._retry_at: dict[str, float] = {}

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
        with self.store.database.read() as connection:
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
        if view.reason == "none":
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
        actions = [name for name in actions if all(item["action"] != name for item in pending)]
        if view.permitted_actions is not None:
            actions = [name for name in actions if name in view.permitted_actions]
        return validate({"profile": SNAPSHOT_PROFILE, "kind": "snapshot", "journey_id": journey_id,
            "generation": session.generation, "revision": revision, "expires_at": session.expires_at,
            "scope_digest": digest({"journey_id": journey_id, "generation": session.generation,
                "revision": revision, "stage": session.stage, "facts": facts, "scope": view.scope, "reason": view.reason,
                "permitted_actions": None if view.permitted_actions is None else sorted(view.permitted_actions)}),
            "next_task": task, "facts": facts, "local_ready": local_ready,
            "full_ready": local_ready and facts["activation"] == "verified" and facts["analysis"] == "verified",
            "pending_operations": pending, "allowed_actions": actions, "reason": view.reason,
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
            expected = command["expected"]
            if (command["journey_id"] != snapshot["journey_id"]
                    or any(expected[name] != snapshot[name] for name in ("generation", "revision", "scope_digest"))):
                raise CommandRefused("setup_changed")
            if command["action"] not in snapshot["allowed_actions"]:
                raise CommandRefused("action_unavailable")
            scope = dict(view.scope)
            action = command["action"]
            if action.startswith("auth.") or action == "account.connect" and "provider" in command:
                from app.persistence.auth import _new_uuid7
                authentication = snapshot["authentication"]
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
            # Retain unresolved work independently of presentation expiry. Bound
            # new commands without preventing reads or native reattachment.
            connection.execute("DELETE FROM desktop_setup_operations WHERE state IN ('completed','refused') AND expires_at<=?", (now,))
            if connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] >= 128:
                raise CommandRefused("operation_limit_reached")
            connection.execute("INSERT OR IGNORE INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)", (journey, now))
            connection.execute("UPDATE desktop_setup_journeys SET entry_source=(SELECT entry_source FROM desktop_presentations "
                "WHERE generation=?) WHERE journey_id=?", (current.generation, journey))
            # The journal cannot silently jump from one selected workspace to another.
            connection.execute("UPDATE desktop_presentations SET journey_id=? WHERE generation=?",
                               (journey, current.generation))
            connection.execute("INSERT INTO desktop_setup_operations VALUES (?,?,?,?,?,?,?,?,'accepted','none',?,?)",
                (command["operation_id"], journey, command["action"], request_digest, current.generation,
                 expected["scope_digest"], expected["revision"], canonical(accepted.scope).decode(), now, current.expires_at))
            connection.execute("UPDATE desktop_setup_journeys SET revision=revision+1 WHERE journey_id=?", (journey,))
            self._current(connection, session)
            row = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (command["operation_id"],)).fetchone()
            return dict(row), True

    async def command(self, session, command, *, actor=None):
        validate(command, kind="command")
        if self._closed:
            raise CommandRefused("setup_unavailable")
        operation, fresh = self._accept(session, command, actor=actor)
        if fresh:
            events.publish()
            self._start(operation, reconcile=False, actor=actor)
        # HTTP cancellation cannot cancel accepted domain work. A duplicate only
        # observes its receipt; it never starts a second worker.
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

    def _start(self, operation, *, reconcile, actor):
        operation_id = operation["operation_id"]
        running = self._workers.get(operation_id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._run(operation, reconcile=reconcile, actor=actor))
        self._workers[operation_id] = task

        def finished(worker):
            if self._workers.get(operation_id) is worker:
                self._workers.pop(operation_id, None)
                self._retry_at[operation_id] = asyncio.get_running_loop().time() + RECOVERY_INTERVAL_SECONDS
                events.publish()

        task.add_done_callback(finished)

    async def _run(self, operation, *, reconcile, actor):
        try:
            result = await self.owner.execute(operation, reconcile=reconcile, actor=actor)
        except asyncio.CancelledError:
            self._settle(operation, OperationResult("outcome_unknown", "operation_unconfirmed"))
            raise
        except Exception:
            # No transport exception proves that a protected operation rolled back.
            result = OperationResult("outcome_unknown", "operation_unconfirmed")
        self._settle(operation, result)
        if result.state != "outcome_unknown":
            await self._refresh_authentication_parent(operation)

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

    async def _watch(self):
        listener = events.subscribe()
        signal = listener[1]
        try:
            while not self._closed:
                signal.clear()
                pending = self._pending()
                pending_ids = {operation["operation_id"] for operation in pending}
                self._retry_at = {key: value for key, value in self._retry_at.items() if key in pending_ids}
                retry_delays = []
                for operation in pending:
                    try:
                        observed = self.owner.observe(operation)
                    except Exception:
                        # One unavailable authority must not stop reconciliation
                        # for every later event. No observation proves rollback.
                        observed = OperationResult("outcome_unknown", "operation_unconfirmed")
                    if observed is not None:
                        self._settle(operation, observed)
                    elif operation["operation_id"] not in self._workers:
                        recover = getattr(self.owner, "automatic_recovery", None)
                        try:
                            recoverable = recover is not None and recover(operation)
                        except Exception:
                            recoverable = False
                        if recoverable:
                            # Only domain-approved recovery of accepted work. No
                            # new command, browser window or presentation is created.
                            clock = asyncio.get_running_loop().time()
                            due = self._retry_at.setdefault(operation["operation_id"], clock + RECOVERY_INTERVAL_SECONDS)
                            if due <= clock:
                                self._start(operation, reconcile=True, actor=None)
                            else:
                                retry_delays.append(due - clock)
                deadlines = [operation["expires_at"] - self.admissions._now() for operation in self._pending()
                    if operation["expires_at"] > self.admissions._now()]
                deadlines.extend(retry_delays)
                if pending:
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
            events.unsubscribe(listener)

    async def drain(self):
        while self._workers:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._workers.values())), return_exceptions=True)

    async def stop(self):
        self.begin_shutdown()
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        await self.drain()

    def begin_shutdown(self):
        """Close presentation admission before transport waits for responses.

        Accepted owner work remains tracked until stop() drains it. This flag
        only refuses new commands/reconciliation and ends presentation streams.
        """
        self._closed = True
        events.publish()
