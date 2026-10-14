"""Authentication observations admitted by distinct helper and paired adapters."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from app.onboarding.admission import AdmissionRefused
from app.security.browser_ceremony import _contract


@dataclass(frozen=True)
class AdmittedAuthenticationTask:
    # Internal, bounded task handle. Its callback rechecks transport admission
    # in the owner's transaction and cannot supply general operator authority.
    task_id: str
    generation: str
    admit: Callable


class AuthenticationTaskReports:
    def __init__(self, coordinator, ceremonies):
        self.coordinator, self.ceremonies = coordinator, ceremonies

    def _operation(self, handle, connection):
        task = handle.admit(connection)
        if (task is None or task["task_id"] != handle.task_id
                or task["admission_generation"] != handle.generation
                or task["purpose"] != "browser.authentication"
                or task["expires_at"] <= self.coordinator.admissions._now()):
            raise AdmissionRefused("task_scope_changed")
        operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (task["operation_id"],)).fetchone()
        if (operation is None or operation["journey_id"] != task["journey_id"]
                or operation["scope_digest"] != task["scope_digest"]):
            raise AdmissionRefused("task_scope_changed")
        return dict(operation)

    def report(self, handle, *, state, notification=None):
        """Read original work or withdraw its exact selected child; never confirm."""
        if self.coordinator._closed or self.ceremonies is None:
            raise AdmissionRefused("task_scope_changed")
        if state == "closed":
            if notification is not None or self.ceremonies.authentication is None:
                raise AdmissionRefused("task_scope_changed")
            operation = self.ceremonies.authentication.close_for_task(handle.task_id, handle.generation,
                authorize=lambda connection: self._operation(handle, connection))
        elif state == "checking":
            _contract().validate("notification", notification)
            with self.coordinator.store.database.read() as connection:
                operation = self._operation(handle, connection)
                saved = self.ceremonies.task_row(operation["operation_id"])
                if (saved is None or notification["ceremony_id"] != saved["ceremony_id"]
                        or notification["attempt_id"] != json.loads(saved["binding_json"])["attempt_id"]):
                    raise AdmissionRefused("task_scope_changed")
        else:
            raise AdmissionRefused("task_scope_changed")
        if operation["state"] in {"accepted", "waiting", "outcome_unknown"}:
            self.coordinator._start(operation, reconcile=True, actor=None)
