"""Bind an explicit desktop pairing intent to its independently delivered code."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import nullcontext
from datetime import datetime, timezone

from app.onboarding.admission import AdmissionRefused
from app.onboarding.coordinator import OperationResult
from app.persistence.auth import AuthenticationStateError
from app.persistence.companion_pairing import CompanionPairingPersistenceError
from app.provisioning.events import events
from app.security.companion_pairing import CompanionPairingService, PairingReservation, encode_pairing_id
from app.security.hosted_grants import HostedGrantUnavailable


class DesktopPairing:
    def __init__(self, store, *, browser_tasks, service: CompanionPairingService):
        self.store, self.browser_tasks, self.service = store, browser_tasks, service
        # Request credentials stay in memory and expire with the original window.
        # After restart only an explicit authenticated native action restores this.
        self._operators = {}
        self._comparisons = set()
        self._closed = False

    def _row(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM desktop_pairing_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return None if row is None else dict(row)

    def _guard(self, operation, authorize, *, session=None, connection=None):
        if self._closed:
            raise AdmissionRefused("task_scope_changed")
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            policy = authorize(connection=current)
            saved = current.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()
            if (saved is None or saved["action"] != "browser.pair"
                    or saved["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or saved["expires_at"] <= self.store._now().timestamp()):
                raise AdmissionRefused("task_scope_changed")
            if session is not None:
                _, task = self.browser_tasks.admissions._require(current, session.identifier,
                    role="browser_task", generation=session.generation)
                if task["purpose"] != "browser.pairing-confirmation" or task["operation_id"] != saved["operation_id"]:
                    raise AdmissionRefused("task_scope_changed")
            return policy

    def _reserve(self, operation, authorize):
        with self.store.database.transaction() as connection:
            self._guard(operation, authorize, connection=connection)
            row = connection.execute("SELECT * FROM desktop_pairing_operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()
            if row is None:
                now = self.store._now().timestamp()
                connection.execute("INSERT INTO desktop_pairing_operations VALUES (?,?,?,?)",
                    (operation["operation_id"], secrets.token_bytes(32), now, min(now + 300, operation["expires_at"])))
                row = connection.execute("SELECT * FROM desktop_pairing_operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()
            return dict(row)

    def observe(self, operation):
        row = self._row(operation["operation_id"])
        if row is None:
            return None
        selected = json.loads(operation["owner_scope_json"])
        persistence = self.service.persistence
        pin = persistence.companion_pin(row["pairing_id"])
        result = None
        if pin is not None:
            try:
                current = persistence.session_authority(row["pairing_id"])
                if (current.pin.creator_account_id != selected["creator_account_id"]
                        or current.pin.installation_id != selected["installation_id"]):
                    raise HostedGrantUnavailable("Pairing scope changed")
                result = OperationResult("completed")
            except (CompanionPairingPersistenceError, AuthenticationStateError, HostedGrantUnavailable):
                result = OperationResult("refused", "operation_refused")
        else:
            window = persistence.window(row["pairing_id"])
            if window is not None and window.state.value in {"cancelled", "declined", "expired", "revoked"}:
                result = OperationResult("refused", "operation_expired" if window.state.value == "expired" else "operation_refused")
            elif row["expires_at"] <= self.store._now().timestamp():
                result = OperationResult("refused", "operation_expired")
        if result is not None:
            self._operators.pop(operation["operation_id"], None)
        return result

    async def execute(self, operation, *, authorize, can_prepare):
        observed = self.observe(operation)
        if observed is not None:
            return observed
        if not can_prepare:
            return OperationResult("outcome_unknown", "operation_unconfirmed")
        if operation["expires_at"] <= self.store._now().timestamp():
            return OperationResult("refused", "operation_expired")
        row = self._reserve(operation, authorize)
        if row["expires_at"] <= self.store._now().timestamp():
            return OperationResult("refused", "operation_expired")
        def guard(connection=None):
            return self._guard(operation, authorize, connection=connection)

        policy = guard()
        selected = json.loads(operation["owner_scope_json"])
        reservation = PairingReservation(row["pairing_id"], datetime.fromtimestamp(row["opened_at"], timezone.utc),
            datetime.fromtimestamp(row["expires_at"], timezone.utc))
        self.service.open(policy, selected["creator_account_id"], reservation=reservation, authorize=guard)
        observed = self.observe(operation)
        if observed is not None:
            return observed
        now = self.store._now().timestamp()
        self._operators = {key: value for key, value in self._operators.items() if value[0] > now}
        self._operators[operation["operation_id"]] = (row["expires_at"], authorize)
        self.browser_tasks.prepare(operation, "browser.pairing-confirmation", deadline=row["expires_at"])
        return OperationResult("waiting")

    async def compare(self, session, operation, value):
        if (not isinstance(value, dict) or set(value) != {"comparison_code"}
                or not isinstance(value["comparison_code"], str) or re.fullmatch(r"[0-9]{6}", value["comparison_code"]) is None):
            raise AdmissionRefused("browser_comparison_invalid")
        observed = self.observe(operation)
        if observed is not None:
            if observed.state == "completed":
                return
            raise AdmissionRefused("task_scope_changed")
        saved = self._row(operation["operation_id"])
        retained = self._operators.get(operation["operation_id"])
        if saved is None or retained is None or retained[0] <= self.store._now().timestamp():
            raise AdmissionRefused("operator_unavailable")
        def guard(connection=None):
            return self._guard(operation, retained[1], session=session, connection=connection)

        identifier = operation["operation_id"]
        if identifier in self._comparisons:
            raise AdmissionRefused("comparison_pending")
        self._comparisons.add(identifier)
        listener = events.subscribe()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(5, saved["expires_at"] - self.store._now().timestamp())
        try:
            while True:
                # The extension's port can deliver its code before the pairing
                # socket commits the agent proof. Subscribe before reading and
                # wait for that exact domain transition without replaying a code.
                listener[1].clear()
                policy = guard()
                window = self.service.persistence.window(saved["pairing_id"])
                if window is None:
                    raise AdmissionRefused("task_scope_changed")
                if window.state.value == "awaiting_confirmation":
                    break
                if window.state.value not in {"open", "offered"}:
                    raise AdmissionRefused("task_scope_changed")
                try:
                    await asyncio.wait_for(listener[1].wait(), timeout=max(0, deadline - loop.time()))
                except TimeoutError:
                    raise AdmissionRefused("comparison_unconfirmed") from None
            # Only the extension-port code enters this method. Brain's own code
            # is never part of the helper payload or response.
            self.service.confirm(policy, encode_pairing_id(saved["pairing_id"]), window.version,
                value["comparison_code"], authorize=guard)
            self._operators.pop(identifier, None)
        finally:
            events.unsubscribe(listener)
            self._comparisons.discard(identifier)

    async def stop(self):
        self._closed = True
        self._operators.clear()
        events.publish()
