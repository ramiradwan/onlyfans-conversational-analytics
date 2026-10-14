"""Durable identity-provider choice within an existing setup operation."""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

from app.onboarding.contract import canonical
from app.onboarding.coordinator import OperationResult
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.browser_authentication import PROFILE, BrowserAuthenticationRefused
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp


class DesktopAuthentication:
    def __init__(self, store, ceremonies, client, *, providers=("default",)):
        if not providers or len(set(providers)) != len(providers) or not set(providers) <= {
                "google", "microsoft-personal", "default"} or "default" not in providers:
            raise ValueError("Invalid authentication providers")
        self.store, self.ceremonies, self.client = store, ceremonies, client
        self.choices = {name: name in providers for name in ("google", "microsoft-personal", "default")}
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()

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
        result = {name: value[name] for name in ("state", "choices", "actions", "authentication_attempt")}
        if timestamp(value["expires_at"]) <= self.store._now():
            result.update(state="expired", actions=dict.fromkeys(value["actions"], False))
        return {"operation_id": parent, "browser_operation_id": browser_operation, **result}

    def parent_operation(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT parent_operation_id FROM desktop_authentication_commands WHERE operation_id=?",
                (operation_id,)).fetchone()
        return operation_id if row is None else row["parent_operation_id"]

    def can_open(self, operation_id):
        return self._open_projection(operation_id) is not None

    def preparation_recovery(self, operation_id):
        value = self._open_projection(operation_id)
        return value is not None and value["state"] == "admitted"

    def _open_projection(self, operation_id):
        with self.store.database.read() as connection:
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

    def _request(self, parent, attempt=None):
        row = self.ceremonies.row(parent)
        if row is None or row["ceremony_id"] is None:
            raise HostedGrantUnavailable("Authentication scope is unavailable")
        return row, {"profile": PROFILE, "operation_id": row["hosted_operation_id"],
            "ceremony_id": row["ceremony_id"], "binding": json.loads(row["binding_json"]),
            "authentication_attempt_id": attempt}

    def _save(self, row, projection):
        encoded = canonical(projection).decode()
        with self.store.database.transaction() as connection:
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
        events.publish()

    async def _read(self, parent, *, authorize, attempt=None):
        row, request = self._request(parent, attempt)
        result = await self._effects.run(self.client.read, request, expires_at=row["provider_expires_at"],
            before_send=authorize, journey_id=self.ceremonies._journey(parent), protected=False)
        if attempt is None:
            self._save(row, result)
        return result

    async def refresh(self, parent, *, authorize):
        async with self._lock:
            await self._recover_closed(parent, authorize=authorize)
            return await self._read(parent, authorize=authorize)

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

    async def _recover_closed(self, parent, *, authorize):
        with self.store.database.read() as connection:
            rows = connection.execute("SELECT c.*,a.request_json FROM desktop_authentication_closures c "
                "JOIN desktop_authentication_commands a ON a.operation_id=c.operation_id "
                "WHERE a.parent_operation_id=? AND c.state='pending'", (parent,)).fetchall()
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
        row, request = self._request(parent, intent["authentication_attempt_id"])
        if self.ceremonies._journey(parent) != operation["journey_id"]:
            raise HostedGrantUnavailable("Authentication journey changed")
        if verb in {"select", "switch"}:
            request.update(provider=intent["provider"], expected_previous_attempt_id=intent["expected_previous_attempt_id"])
        encoded = canonical(request).decode()
        with self.store.database.transaction() as connection:
            previous = connection.execute("SELECT * FROM desktop_authentication_commands WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()
            if previous is not None:
                if (previous["request_json"], previous["verb"], previous["parent_operation_id"]) != (encoded, verb, parent):
                    raise HostedGrantUnavailable("Authentication command changed")
                return row, request, verb, False
            connection.execute("INSERT INTO desktop_authentication_commands VALUES (?,?,?,?)",
                (operation["operation_id"], parent, verb, encoded))
        return row, request, verb, True

    async def execute(self, operation, *, authorize, read_guard, reconcile):
        async with self._lock:
            row, request, verb, fresh = self._reserve(operation)
            parent = row["operation_id"]
            await self._recover_closed(parent, authorize=read_guard)
            if fresh and not reconcile:
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

    async def ensure_initial(self, operation, *, authorize, read_guard):
        intent = json.loads(operation["owner_scope_json"]).get("authentication_intent")
        if intent is not None:
            with self.store.database.read() as connection:
                exists = connection.execute("SELECT 1 FROM desktop_authentication_commands WHERE operation_id=?",
                    (operation["operation_id"],)).fetchone() is not None
            if not exists:
                await self.execute(operation, authorize=authorize, read_guard=read_guard, reconcile=False)
        return await self.refresh(operation["operation_id"], authorize=read_guard)

    async def stop(self):
        self._effects.close_admission()
        await self._effects.drain()
        close = getattr(self.client.transport, "close", None)
        if close is not None:
            close()
