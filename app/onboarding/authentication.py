"""Durable identity-provider choice within an existing setup operation."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext

from app.onboarding import timing
from app.onboarding.contract import canonical
from app.onboarding.coordinator import OperationResult
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.browser_authentication import PROFILE, BrowserAuthenticationRefused
from app.security.browser_ceremony_wait import wait_request
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp

PROBE_INTERVAL_SECONDS = 2
SETTLED_SECONDS = 5


class DesktopAuthentication:
    def __init__(self, store, ceremonies, client, *, providers=("default",), waits=None):
        if not providers or len(set(providers)) != len(providers) or not set(providers) <= {
                "google", "microsoft-personal", "telegram", "tiktok", "default"} or "default" not in providers:
            raise ValueError("Invalid authentication providers")
        self.store, self.ceremonies, self.client, self.waits = store, ceremonies, client, waits
        self.choices = {name: name in providers for name in ("google", "microsoft-personal", "telegram", "tiktok", "default")}
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()
        self._probed: dict[str, str] = {}
        self._prefetched = {}
        self._settled = {}

    def presentation(self, journey_id, action, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            rows = current.execute(
                "SELECT o.operation_id,a.projection_json FROM desktop_setup_operations o "
                "LEFT JOIN desktop_authentication_projections a ON a.operation_id=o.operation_id "
                "WHERE o.journey_id=? AND o.action=? AND o.state IN ('accepted','waiting','outcome_unknown')",
                (journey_id, action)).fetchall()
            if not rows:
                if action != "account.connect":
                    return None
                return {"operation_id": None, "browser_operation_id": None, "state": "selection-required",
                    "choices": dict(self.choices), "actions": {"select": True, "cancel": False,
                        "open_browser": False, "switch_account": False}, "authentication_attempt": None}
            if len(rows) != 1 or rows[0]["projection_json"] is None:
                return None
            parent = rows[0]["operation_id"]
            value = json.loads(rows[0]["projection_json"])
            attempt = value["authentication_attempt"]
            browser_operation = None
            if attempt is not None:
                for row in current.execute("SELECT operation_id,request_json FROM desktop_authentication_commands "
                        "WHERE parent_operation_id=? AND verb IN ('select','switch')", (parent,)).fetchall():
                    if json.loads(row["request_json"])["authentication_attempt_id"] == attempt["authentication_attempt_id"]:
                        browser_operation = row["operation_id"]
                        break
            ended = browser_operation is not None and current.execute(
                "SELECT 1 FROM desktop_browser_tasks t LEFT JOIN desktop_authentication_closures c "
                "ON c.task_id=t.task_id AND c.operation_id=t.operation_id "
                "WHERE t.operation_id=? AND t.purpose='browser.authentication' "
                "AND (t.state IN ('completed','cancelled','expired') OR c.operation_id IS NOT NULL)",
                (browser_operation,)).fetchone() is not None
        result = {name: value[name] for name in ("state", "choices", "actions", "authentication_attempt")}
        if timestamp(value["expires_at"]) <= self.store._now():
            result.update(state="expired", actions=dict.fromkeys(value["actions"], False))
        elif ended and value["state"] == "selected":
            result.update(state="reauthentication-required",
                actions=dict(select=True, cancel=False, open_browser=False, switch_account=False))
        return {"operation_id": parent, "browser_operation_id": browser_operation, **result}

    def parent_operation(self, operation_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            row = connection.execute("SELECT parent_operation_id FROM desktop_authentication_commands WHERE operation_id=?",
                (operation_id,)).fetchone()
        return operation_id if row is None else row["parent_operation_id"]

    def can_open(self, operation_id, *, connection=None):
        return self._open_projection(operation_id, connection=connection) is not None

    def preparation_recovery(self, operation_id, *, connection=None):
        value = self._open_projection(operation_id, connection=connection)
        return value is not None and value["state"] == "admitted"

    def _open_projection(self, operation_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            selected = connection.execute("SELECT * FROM desktop_authentication_commands WHERE operation_id=?",
                (operation_id,)).fetchone()
            if selected is None or selected["verb"] == "cancel":
                return None
            row = connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
                (selected["parent_operation_id"],)).fetchone()
        if row is None:
            return None
        value, request = json.loads(row["projection_json"]), json.loads(selected["request_json"])
        attempt = value["authentication_attempt"]
        return value if (value["actions"]["open_browser"] and timestamp(value["expires_at"]) > self.store._now()
            and attempt is not None and attempt["authentication_attempt_id"] == request["authentication_attempt_id"]) else None

    def _request(self, parent, attempt=None, *, connection=None):
        row = self.ceremonies.row(parent) if connection is None else self.ceremonies.row(parent, connection=connection)
        if row is None or row["ceremony_id"] is None:
            raise HostedGrantUnavailable("Authentication scope is unavailable")
        return row, {"profile": PROFILE, "operation_id": row["hosted_operation_id"],
            "ceremony_id": row["ceremony_id"], "binding": json.loads(row["binding_json"]),
            "authentication_attempt_id": attempt}

    def _save(self, row, projection, *, connection=None):
        encoded = canonical(projection).decode()
        publish = connection is None
        with (self.store.database.transaction() if connection is None else nullcontext(connection)) as connection:
            current = connection.execute("SELECT binding_json,ceremony_id FROM desktop_browser_ceremonies WHERE operation_id=?",
                (row["operation_id"],)).fetchone()
            if current is None or (current["binding_json"], current["ceremony_id"]) != (row["binding_json"], row["ceremony_id"]):
                raise HostedGrantUnavailable("Authentication scope changed")
            previous = connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
                (row["operation_id"],)).fetchone()
            if previous is not None and previous[0] == encoded:
                return
            connection.execute("INSERT INTO desktop_authentication_projections VALUES (?,?,1) "
                "ON CONFLICT(operation_id) DO UPDATE SET projection_json=excluded.projection_json,revision=revision+1",
                (row["operation_id"], encoded))
            connection.execute("DELETE FROM desktop_decision_projections WHERE operation_id=? "
                "AND json_extract(projection_json,'$.state')='confirmation-required'", (row["operation_id"],))
        if publish:
            events.publish()

    def _projection(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
                (operation_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    async def _read(self, parent, *, authorize, attempt=None):
        row, request = self._request(parent, attempt)
        with timing.phase("authentication-read", "read-end"):
            result = await self._read_request(row, request, authorize=authorize)
        if attempt is None:
            self._save(row, result)
        return result

    async def _read_request(self, row, request, *, authorize, journey_id=None):
        ticket = self._prefetched.pop(row["operation_id"], None) if request["authentication_attempt_id"] is None else None
        options = {} if ticket is None else {"prefetched": ticket}
        journey_id = self.ceremonies._journey(row["operation_id"]) if journey_id is None else journey_id
        return await self._effects.run(self.client.read, request, expires_at=row["provider_expires_at"],
            before_send=authorize, journey_id=journey_id, protected=False, **options)

    async def prefetch(self, operation_id, *, authorize):
        """Hold one fresh current-read challenge. Returns seconds until it needs renewal."""
        if not hasattr(self.client, "prefetch_read"):
            return None
        parent = self.parent_operation(operation_id)
        row, request = self._request(parent)
        renewal = getattr(self.client, "renewal_delay", None)
        held = self._prefetched.get(parent)
        if held is not None:
            if renewal is None:
                return None
            delay = renewal(held)
            if delay > 0:
                return delay
        with timing.phase("authentication-read", "renew"):
            ticket = await self._effects.run(self.client.prefetch_read, request, before_send=authorize,
                journey_id=self.ceremonies._journey(parent), protected=False)
        if self._prefetched.get(parent) in (None, held):
            self._prefetched.pop(parent, None)
            if len(self._prefetched) >= 64:
                self._prefetched.pop(next(iter(self._prefetched)))
            self._prefetched[parent] = ticket
        return None if renewal is None else renewal(ticket)

    async def reconcile_task(self, operation_id, *, authorize):
        """Only a current proof read can complete this exact live task generation."""
        async with self._lock:
            with self.store.database.read() as connection:
                tasks = connection.execute("SELECT * FROM desktop_browser_tasks WHERE operation_id=? "
                    "AND purpose='browser.authentication' AND state IN ('pending','accepted','outcome_unknown')",
                    (operation_id,)).fetchall()
                command = connection.execute("SELECT * FROM desktop_authentication_commands WHERE operation_id=?",
                    (operation_id,)).fetchone()
                closure = connection.execute("SELECT 1 FROM desktop_authentication_closures WHERE operation_id=? "
                    "AND state='pending'", (operation_id,)).fetchone()
                if len(tasks) != 1 or command is None or command["verb"] == "cancel" or closure is not None:
                    return None
                # The hinted read's scope comes from the same connection as the task it may complete.
                row, request = self._request(command["parent_operation_id"], connection=connection)
                journey = self.ceremonies._journey(row["operation_id"], connection=connection)
            task = dict(tasks[0])
            with timing.phase("authentication-read", "read-end"):
                result = await self._read_request(row, request, authorize=authorize, journey_id=journey)
            expected = json.loads(command["request_json"])
            attempt = result["authentication_attempt"]
            return await self._effects.run(self._finish_task, operation_id, task, row, result, expected, attempt,
                authorize=authorize, journey_id=journey, protected=False)

    def _finish_task(self, operation_id, task, row, result, expected, attempt, *, authorize):
        closed = False
        with self.store.database.transaction() as connection:
            authorize()
            current = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
            closure = connection.execute("SELECT 1 FROM desktop_authentication_closures WHERE operation_id=? "
                "AND state='pending'", (operation_id,)).fetchone()
            if (current is None or any(current[name] != task[name] for name in (
                    "admission_generation", "operation_id", "journey_id", "scope_digest", "expires_at"))
                    or current["state"] not in {"pending", "accepted", "outcome_unknown"}
                    or current["expires_at"] <= self.store._now().timestamp()
                    or operation is None or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or operation["journey_id"] != task["journey_id"] or operation["scope_digest"] != task["scope_digest"]
                    or closure is not None):
                return None
            self._save(row, result, connection=connection)
            if (result["state"] == "admitted" and not result["actions"]["open_browser"]
                    and timestamp(result["expires_at"]) > self.store._now() and attempt is not None
                    and attempt["authentication_attempt_id"] == expected["authentication_attempt_id"]):
                connection.execute("UPDATE desktop_browser_tasks SET state='completed' WHERE task_id=? "
                    "AND admission_generation=?", (task["task_id"], task["admission_generation"]))
                closed = True
            else:
                still_open = (result["actions"]["open_browser"] and timestamp(result["expires_at"]) > self.store._now()
                    and attempt is not None and attempt["authentication_attempt_id"] == expected["authentication_attempt_id"])
        if closed:
            timing.completed(operation_id)
            # The parent's next refresh can use this projection instead of reading it again.
            self._settled.pop(row["operation_id"], None)
            if len(self._settled) >= 64:
                self._settled.pop(next(iter(self._settled)))
            self._settled[row["operation_id"]] = (time.monotonic(), canonical(result).decode(),
                (row["binding_json"], row["ceremony_id"]))
        events.publish()
        return True if closed else False if still_open else None

    def probe_interval(self, operation_id):
        """Probe cadence while this operation's sign-in popup can still be open."""
        with self.store.database.read() as connection:
            parent = self.parent_operation(operation_id, connection=connection)
            ceremony = self.ceremonies.row(parent, connection=connection)
            if ceremony is None or ceremony["ceremony_id"] is None:
                return None
            if self.confirmation_deadline(operation_id, connection=connection) is not None:
                return PROBE_INTERVAL_SECONDS
            row = connection.execute("SELECT 1 FROM desktop_browser_tasks WHERE operation_id=? "
                "AND purpose='browser.authentication' AND state IN ('pending','accepted','outcome_unknown') "
                "AND expires_at>? LIMIT 1", (operation_id, self.store._now().timestamp())).fetchone()
        return None if row is None else PROBE_INTERVAL_SECONDS

    def confirmation_deadline(self, operation_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as connection:
            parent = self.parent_operation(operation_id, connection=connection)
            row = connection.execute("SELECT d.projection_json FROM desktop_decision_projections d "
                "JOIN desktop_setup_operations o ON o.operation_id=d.operation_id WHERE d.operation_id=? "
                "AND o.state IN ('accepted','waiting','outcome_unknown')", (parent,)).fetchone()
        if row is None:
            return None
        projection = json.loads(row["projection_json"])
        if projection["state"] != "confirmation-required":
            return None
        deadline = timestamp(projection["decision"]["expires_at"]).timestamp()
        return deadline if deadline > self.store._now().timestamp() else None

    def waiting_for_sign_in(self, operation_id):
        """A known ended decision needs another explicit selection, not a retry."""
        with self.store.database.read() as connection:
            parent = self.parent_operation(operation_id, connection=connection)
            if self._closures(connection, parent):
                return False
            row = connection.execute("SELECT projection_json FROM desktop_decision_projections WHERE operation_id=?",
                (parent,)).fetchone()
            decision = None if row is None else json.loads(row[0])
            if decision is not None and decision["state"] == "accepted":
                return False
            if self.can_open(operation_id, connection=connection):
                return False
            if (decision is not None and decision["state"] == "confirmation-required"
                    and timestamp(decision["decision"]["expires_at"]) <= self.store._now()):
                return True
            row = connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
                (parent,)).fetchone()
        if row is None:
            return False
        authentication = json.loads(row[0])
        return (authentication["state"] in {"selection-required", "reauthentication-required", "expired", "unavailable"}
            or timestamp(authentication["expires_at"]) <= self.store._now())

    async def probe(self, operation_id, *, authorize):
        """Read the parent's current projection only. True when it differs from the last probe."""
        parent = self.parent_operation(operation_id)
        row, request = self._request(parent)
        result = await self._effects.run(self.client.read, request, expires_at=row["provider_expires_at"],
            before_send=authorize, journey_id=self.ceremonies._journey(parent), protected=False)
        encoded = canonical(result).decode()
        if self._probed.get(operation_id) == encoded:
            return False
        self._probed.pop(operation_id, None)
        if len(self._probed) >= 64:
            self._probed.pop(next(iter(self._probed)))
        self._probed[operation_id] = encoded
        return True

    async def wait(self, operation_id, *, authorize):
        """Hold the parent's ceremony outcome wait. Each hint is a notification without authority."""
        if self.waits is None:
            raise HostedGrantUnavailable("Ceremony wait is unavailable")
        parent = self.parent_operation(operation_id)
        row = self.ceremonies.row(parent)
        if row is None or row["ceremony_id"] is None:
            raise HostedGrantUnavailable("Authentication scope is unavailable")
        request = wait_request(self.ceremonies.request(row))
        envelope = await asyncio.to_thread(self.waits.envelope, request, before_send=authorize)
        async for hint in self.waits.hints(envelope):
            yield hint

    async def refresh(self, parent, *, authorize):
        async with self._lock:
            with self.store.database.read() as connection:
                rows = self._closures(connection, parent)
                settled = None if rows else self._settled_projection(parent, connection)
            if settled is not None:
                return settled
            await self._recover_closed(parent, authorize=authorize, rows=rows)
            return await self._read(parent, authorize=authorize)

    def _settled_projection(self, parent, connection):
        """The projection a hinted read just saved when it completed the task, once and only while unchanged."""
        settled = self._settled.pop(parent, None)
        if settled is None or time.monotonic() - settled[0] > SETTLED_SECONDS:
            return None
        current = connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
            (parent,)).fetchone()
        ceremony = self.ceremonies.row(parent, connection=connection)
        if (current is None or current["projection_json"] != settled[1] or ceremony is None
                or (ceremony["binding_json"], ceremony["ceremony_id"]) != settled[2]):
            return None
        value = json.loads(current["projection_json"])
        return value if timestamp(value["expires_at"]) > self.store._now() else None

    def close_for_task(self, task_id, generation, *, authorize):
        """Accept withdrawal of one admitted browser task, never a native command."""
        with self.store.database.transaction() as connection:
            authorize(connection)
            task = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task_id,)).fetchone()
            now = self.store._now().timestamp()
            if (task is None or task["admission_generation"] != generation
                    or task["purpose"] != "browser.authentication" or task["expires_at"] <= now
                    or task["state"] not in {"pending", "accepted", "outcome_unknown"}):
                raise HostedGrantUnavailable("Authentication task changed")
            command = connection.execute("SELECT * FROM desktop_authentication_commands WHERE operation_id=?",
                (task["operation_id"],)).fetchone()
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                (task["operation_id"],)).fetchone()
            parent = None if command is None else connection.execute(
                "SELECT * FROM desktop_setup_operations WHERE operation_id=?", (command["parent_operation_id"],)).fetchone()
            if (command is None or command["verb"] not in {"select", "switch"} or operation is None or parent is None
                    or operation["journey_id"] != task["journey_id"] or parent["journey_id"] != task["journey_id"]
                    or operation["scope_digest"] != task["scope_digest"]
                    or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or parent["state"] not in {"accepted", "waiting", "outcome_unknown"}):
                raise HostedGrantUnavailable("Authentication owner changed")
            connection.execute("DELETE FROM desktop_authentication_closures WHERE expires_at<=?", (now,))
            deadline = min(task["expires_at"], operation["expires_at"], parent["expires_at"],
                timestamp(json.loads(command["request_json"])["binding"]["source_expires_at"]).timestamp())
            saved = connection.execute("SELECT * FROM desktop_authentication_closures WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()
            if saved is None:
                if deadline <= now or connection.execute("SELECT count(*) FROM desktop_authentication_closures").fetchone()[0] >= 128:
                    raise HostedGrantUnavailable("Authentication closure is unavailable")
                connection.execute("INSERT INTO desktop_authentication_closures VALUES (?,?,?,'pending')",
                    (operation["operation_id"], task_id, deadline))
            elif saved["task_id"] != task_id or saved["expires_at"] != deadline:
                raise HostedGrantUnavailable("Authentication closure changed")
        events.publish()
        return dict(operation)

    @staticmethod
    def _closures(connection, parent):
        return connection.execute("SELECT c.*,a.request_json FROM desktop_authentication_closures c "
            "JOIN desktop_authentication_commands a ON a.operation_id=c.operation_id "
            "WHERE a.parent_operation_id=? AND c.state='pending'", (parent,)).fetchall()

    async def _recover_closed(self, parent, *, authorize, rows=None):
        if rows is None:
            with self.store.database.read() as connection:
                rows = self._closures(connection, parent)
        for pending in rows:
            request = json.loads(pending["request_json"])
            row, expected = self._request(parent, request["authentication_attempt_id"])
            if any(request[name] != value for name, value in expected.items()):
                raise HostedGrantUnavailable("Authentication closure scope changed")
            # The retained owner/key fences outlive the helper. Provider alone
            # decides whether this exact child is still cancellable.
            current = await self._read(parent, authorize=authorize)
            attempt = current["authentication_attempt"]
            if (self.store._now().timestamp() < pending["expires_at"] and current["state"] == "selected"
                    and current["actions"]["cancel"] and attempt is not None
                    and attempt["authentication_attempt_id"] == expected["authentication_attempt_id"]):
                def guard():
                    authorize()
                    if self.store._now().timestamp() >= pending["expires_at"]:
                        raise HostedGrantUnavailable("Authentication closure expired")
                try:
                    await self._effects.run(self.client.execute, "cancel", expected,
                        expires_at=row["provider_expires_at"], before_send=guard,
                        journey_id=self.ceremonies._journey(parent))
                except BrowserAuthenticationRefused:
                    pass
                current = await self._read(parent, authorize=authorize)
                attempt = current["authentication_attempt"]
            if (current["state"] in {"selection-required", "retired", "admitted", "expired"}
                    or attempt is not None and attempt["authentication_attempt_id"] != expected["authentication_attempt_id"]):
                with self.store.database.transaction() as connection:
                    connection.execute("UPDATE desktop_authentication_closures SET state='settled' WHERE operation_id=?",
                        (pending["operation_id"],))

    def _reserve(self, operation):
        intent = json.loads(operation["owner_scope_json"])["authentication_intent"]
        parent, verb = intent["parent_operation_id"], intent["verb"]
        # One transaction reads the scope and pending closures and records the command.
        with self.store.database.transaction() as connection:
            row, request = self._request(parent, intent["authentication_attempt_id"], connection=connection)
            if self.ceremonies._journey(parent, connection=connection) != operation["journey_id"]:
                raise HostedGrantUnavailable("Authentication journey changed")
            if verb in {"select", "switch"}:
                request.update(provider=intent["provider"], expected_previous_attempt_id=intent["expected_previous_attempt_id"])
            encoded = canonical(request).decode()
            closures = self._closures(connection, parent)
            previous = connection.execute("SELECT * FROM desktop_authentication_commands WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()
            if previous is not None:
                if (previous["request_json"], previous["verb"], previous["parent_operation_id"]) != (encoded, verb, parent):
                    raise HostedGrantUnavailable("Authentication command changed")
                return row, request, verb, False, closures
            connection.execute("INSERT INTO desktop_authentication_commands VALUES (?,?,?,?)",
                (operation["operation_id"], parent, verb, encoded))
        return row, request, verb, True, closures

    async def execute(self, operation, *, authorize, read_guard, reconcile, use_selection=False):
        async with self._lock:
            label = "authentication-" + json.loads(operation["owner_scope_json"])["authentication_intent"]["verb"]
            # The command is durable before its send. The write runs off the event loop.
            with timing.phase(label, "reserve"):
                row, request, verb, fresh, closures = await asyncio.to_thread(self._reserve, operation)
            parent = row["operation_id"]
            await self._recover_closed(parent, authorize=read_guard, rows=closures)
            if fresh and not reconcile:
                with timing.phase(label, "guard"):
                    authorize()
                if self.store._now().timestamp() >= operation["expires_at"]:
                    return OperationResult("refused", "operation_expired")
                try:
                    result = await self._effects.run(self.client.execute, verb, request,
                        expires_at=row["provider_expires_at"], before_send=authorize, journey_id=operation["journey_id"])
                except BrowserAuthenticationRefused:
                    await self._read(parent, authorize=read_guard)
                    return OperationResult("refused", "operation_refused")
            else:
                # A reserved request may have committed. Reopening only reads
                # that child; a fresh explicit command admits any next attempt.
                result = await self._read(parent, authorize=read_guard, attempt=request["authentication_attempt_id"])
            if use_selection and fresh and not reconcile:
                await asyncio.to_thread(self._save, row, result)
                current = result
            else:
                current = await self._read(parent, authorize=read_guard)
            if (result["state"] == "admitted" and verb != "cancel" and current["actions"]["open_browser"]
                    and current["authentication_attempt"] is not None
                    and current["authentication_attempt"]["authentication_attempt_id"] == request["authentication_attempt_id"]):
                return OperationResult("waiting")
            if result["state"] == "admitted" or verb == "cancel" and result["state"] in {"retired", "expired"}:
                return OperationResult("completed")
            if result["state"] == "selected":
                return OperationResult("waiting")
            if result["state"] == "retired":
                return OperationResult("refused")
            if current["state"] == "selection-required" or result["state"] in {"retired", "expired", "unavailable"}:
                return OperationResult("refused", "operation_expired" if result["state"] == "expired" else "operation_unconfirmed")
            return OperationResult("outcome_unknown", "operation_unconfirmed")

    async def ensure_initial(self, operation, *, authorize, read_guard, fresh=False, current=None):
        """`current` is a projection the caller just read. Without a command to send, it is returned as is."""
        intent = json.loads(operation["owner_scope_json"]).get("authentication_intent")
        if intent is not None:
            def lookup():
                with self.store.database.read() as connection:
                    return connection.execute("SELECT 1 FROM desktop_authentication_commands WHERE operation_id=?",
                        (operation["operation_id"],)).fetchone() is not None
            with timing.phase("authentication-" + intent["verb"], "lookup"):
                exists = await asyncio.to_thread(lookup)
            if not exists:
                await self.execute(operation, authorize=authorize, read_guard=read_guard, reconcile=False, use_selection=fresh)
                if fresh:
                    return await asyncio.to_thread(self._projection, operation["operation_id"])
                current = None
        if current is not None:
            return current
        return await self.refresh(operation["operation_id"], authorize=read_guard)

    async def stop(self):
        self._effects.close_admission()
        await self._effects.drain()
        close = getattr(self.client.transport, "close", None)
        if close is not None:
            close()
