"""Durable, role-separated local presentation admission; never operator authority."""
from __future__ import annotations

import base64
import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from app.persistence.onboarding import require_journey

Role = Literal["desktop", "browser_task"]
Stage = Literal["setup", "runtime"]
PURPOSES = frozenset({"browser.access", "browser.identity", "browser.authentication",
                     "browser.pairing-confirmation", "browser.history-permission"})
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_ADMISSIONS = 128


class AdmissionRefused(ValueError):
    """Expired, replaced or mismatched local presentation authority."""


def _digest(value: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise AdmissionRefused("admission_invalid")
    return hashlib.sha256(b"desktop-admission-v1\0" + value.encode()).hexdigest()


def csrf_token(identifier: str) -> str:
    _digest(identifier)
    return base64.urlsafe_b64encode(hashlib.sha256(
        b"desktop-admission-csrf-v1\0" + identifier.encode()).digest()).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class AdmittedSession:
    identifier: str
    role: Role
    stage: Stage
    generation: str
    journey_id: str | None
    task_id: str | None
    expires_at: float


class ScopedAdmissions:
    def __init__(self, authentication, *, stage: Stage):
        if stage not in {"setup", "runtime"}:
            raise ValueError("invalid desktop stage")
        self.authentication = authentication
        self.stage = stage

    def _now(self):
        return self.authentication._now().timestamp()

    def presentation_current(self, generation: str, journey_id: str | None) -> bool:
        require_journey(generation)
        if journey_id is not None:
            require_journey(journey_id)
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT * FROM desktop_presentations WHERE generation=?", (generation,)).fetchone()
        return bool(row is not None and row["state"] == "active" and row["stage"] == self.stage
            and row["expires_at"] > self._now() and (journey_id is None or row["journey_id"] == journey_id))

    def prepare_desktop(self, journey_id: str | None = None, *, replacing_generation: str | None = None) -> str:
        """A verified launcher may request a presentation, without choosing an account."""
        if journey_id is not None:
            require_journey(journey_id)
        entry_source = "extension" if journey_id is not None else "desktop"
        with self.authentication.database.transaction() as connection:
            now = self._now()
            if replacing_generation is not None:
                require_journey(replacing_generation)
                successor = connection.execute("SELECT * FROM desktop_presentations WHERE refresh_source=?",
                    (replacing_generation,)).fetchone()
                if successor is not None:
                    if (successor["stage"] != self.stage or successor["state"] not in {"active", "pending"}
                            or successor["expires_at"] <= now or connection.execute(
                                "SELECT 1 FROM desktop_presentations WHERE generation NOT IN (?,?) "
                                "AND state IN ('active','pending')", (replacing_generation, successor["generation"])).fetchone()):
                        raise AdmissionRefused("presentation_replaced")
                    # A lost prepare or consume reply may recover only this exact
                    # successor. Reissue a one-use entry, never extend its lifetime.
                    connection.execute("DELETE FROM desktop_admission_entries WHERE role='desktop'")
                    return self._entry(connection, "desktop", successor["generation"], successor["generation"],
                        successor["expires_at"], now)
                previous = connection.execute("SELECT * FROM desktop_presentations WHERE generation=?",
                    (replacing_generation,)).fetchone()
                if (previous is None or previous["stage"] != self.stage
                        or previous["state"] not in {"active", "retired"}
                        or connection.execute("SELECT 1 FROM desktop_presentations WHERE generation<>? "
                            "AND state IN ('active','pending')", (replacing_generation,)).fetchone() is not None):
                    raise AdmissionRefused("presentation_replaced")
                journey_id = previous["journey_id"]
                entry_source = previous["entry_source"]
                # Keep only the immediate retry lineage, not an unbounded chain.
                connection.execute("UPDATE desktop_presentations SET refresh_source=NULL WHERE generation=?",
                    (replacing_generation,))
            saved = connection.execute("SELECT journey_id,entry_source FROM desktop_setup_journeys LIMIT 2").fetchall()
            if saved:
                if len(saved) > 1 or (journey_id is not None and journey_id != saved[0]["journey_id"]):
                    raise AdmissionRefused("setup_selection_required")
                # Resume the coordinator's saved locator, without choosing
                # an account or extending its domain operation authority.
                journey_id = saved[0]["journey_id"]
                entry_source = saved[0]["entry_source"]
            # Native recovery must remain available even when unresolved tasks fill
            # the task-creation budget. Only the newest unconsumed launcher entry
            # is useful; an active presentation stays valid until its replacement
            # is consumed.
            connection.execute("DELETE FROM desktop_admission_entries WHERE role='desktop'")
            connection.execute("UPDATE desktop_presentations SET state='retired' WHERE state='pending'")
            self._cleanup(connection, now)
            generation = str(uuid4())
            connection.execute("INSERT INTO desktop_presentations (generation,journey_id,stage,state,expires_at,entry_source,refresh_source) "
                               "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                               (generation, journey_id, self.stage, now + 1800, entry_source, replacing_generation))
            return self._entry(connection, "desktop", generation, generation, now + 1800, now)

    def _entry(self, connection, role, reference_id, generation, deadline, now):
        code = secrets.token_urlsafe(32)
        connection.execute("INSERT INTO desktop_admission_entries VALUES (?, ?, ?, ?, ?, ?)",
                           (_digest(code), role, reference_id, generation, self.stage, min(now + 300, deadline)))
        return code

    def consume(self, code: str, *, role: Role) -> AdmittedSession:
        with self.authentication.database.transaction() as connection:
            now = self._now()
            row = connection.execute("SELECT * FROM desktop_admission_entries WHERE code_digest=?",
                                     (_digest(code),)).fetchone()
            if row is None or row["role"] != role or row["stage"] != self.stage or row["expires_at"] <= now:
                raise AdmissionRefused("entry_invalid")
            reference = self._reference(connection, row, now, pending_desktop=True)
            if role == "desktop":
                connection.execute("UPDATE desktop_presentations SET state='retired' WHERE generation<>?", (row["generation"],))
                connection.execute("UPDATE desktop_presentations SET state='active' WHERE generation=?", (row["generation"],))
                # A pending earlier launcher entry must not supersede this owner later.
                connection.execute("DELETE FROM desktop_admission_entries WHERE role='desktop'")
                connection.execute("DELETE FROM desktop_admission_sessions WHERE role='desktop'")
            else:
                connection.execute("DELETE FROM desktop_admission_entries WHERE code_digest=?", (_digest(code),))
            identifier = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO desktop_admission_sessions VALUES (?, ?, ?, ?, ?, ?)",
                               (_digest(identifier), role, row["reference_id"], row["generation"], self.stage, reference["expires_at"]))
            return self._session(identifier, row, reference)

    def require(self, identifier: str, *, role: Role, generation: str | None = None) -> AdmittedSession:
        with self.authentication.database.read() as connection:
            row, reference = self._require(connection, identifier, role=role, generation=generation)
            return self._session(identifier, row, reference)

    def _require(self, connection, identifier, *, role, generation=None):
        row = connection.execute("SELECT * FROM desktop_admission_sessions WHERE session_digest=?",
                                 (_digest(identifier),)).fetchone()
        now = self._now()
        if (row is None or row["role"] != role or row["stage"] != self.stage or row["expires_at"] <= now
                or (generation is not None and row["generation"] != generation)):
            raise AdmissionRefused("session_invalid")
        return row, self._reference(connection, row, now)

    def _reference(self, connection, row, now, *, pending_desktop=False):
        if row["role"] == "desktop":
            reference = connection.execute("SELECT * FROM desktop_presentations WHERE generation=?",
                                           (row["reference_id"],)).fetchone()
            if (reference is None or reference["state"] not in ({"pending", "active"} if pending_desktop and reference["refresh_source"]
                        else {"pending"} if pending_desktop else {"active"})
                    or reference["expires_at"] <= now or reference["stage"] != self.stage
                    or reference["generation"] != row["generation"]):
                raise AdmissionRefused("presentation_replaced")
            return reference
        reference = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (row["reference_id"],)).fetchone()
        if (reference is None or reference["expires_at"] <= now or reference["stage"] != self.stage
                or reference["admission_generation"] != row["generation"]
                or reference["state"] in {"cancelled", "expired"}):
            raise AdmissionRefused("browser_task_unavailable")
        parent = connection.execute("SELECT * FROM desktop_presentations WHERE generation=?",
                                    (reference["desktop_generation"],)).fetchone()
        if parent is None or parent["state"] != "active" or parent["expires_at"] <= now:
            raise AdmissionRefused("presentation_replaced")
        return reference

    @staticmethod
    def _session(identifier, row, reference):
        return AdmittedSession(identifier, row["role"], row["stage"], row["generation"],
                               reference["journey_id"], reference["task_id"] if row["role"] == "browser_task" else None,
                               reference["expires_at"])

    def prepare_task(self, identifier: str, *, generation: str, journey_id: str, operation_id: str,
                     purpose: str, scope_digest: str, source_revision: int, deadline: float) -> tuple[str, str]:
        """Called by the coordinator after domain authorization, never by passive reads."""
        require_journey(journey_id)
        require_journey(operation_id)
        if (purpose not in PURPOSES or not isinstance(scope_digest, str) or DIGEST.fullmatch(scope_digest) is None
                or type(source_revision) is not int or source_revision < 0
                or isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise AdmissionRefused("task_scope_invalid")
        with self.authentication.database.transaction() as connection:
            row, parent = self._require(connection, identifier, role="desktop", generation=generation)
            now = self._now()
            self._cleanup(connection, now)
            self._capacity(connection)
            if parent["journey_id"] != journey_id:
                raise AdmissionRefused("task_scope_changed")
            # An operation is immutable. Lost replies use exact read/reissue, never a new task.
            if connection.execute("SELECT 1 FROM desktop_browser_tasks WHERE operation_id=?", (operation_id,)).fetchone():
                raise AdmissionRefused("task_already_exists")
            expires = min(now + 1800, parent["expires_at"], deadline)
            if expires <= now:
                raise AdmissionRefused("task_expired")
            task_id, admission_generation = str(uuid4()), str(uuid4())
            connection.execute("INSERT INTO desktop_browser_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                               (task_id, journey_id, row["generation"], operation_id, purpose, scope_digest,
                                source_revision, admission_generation, self.stage, expires))
            code = self._entry(connection, "browser_task", task_id, admission_generation, expires, now)
            return task_id, code

    def reissue_task_entry(self, identifier: str, *, generation: str, task_id: str) -> str:
        return self._rebind_task(identifier, generation=generation, task_id=task_id, browser_entry=True)

    def rebind_paired_task(self, identifier: str, *, generation: str, task_id: str) -> str:
        """Fence an old helper without minting a browser credential for a paired session."""
        return self._rebind_task(identifier, generation=generation, task_id=task_id, browser_entry=False)

    def _rebind_task(self, identifier, *, generation, task_id, browser_entry):
        require_journey(task_id)
        with self.authentication.database.transaction() as connection:
            _, parent = self._require(connection, identifier, role="desktop", generation=generation)
            now = self._now()
            task = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task_id,)).fetchone()
            if (task is None or task["journey_id"] != parent["journey_id"] or task["stage"] != self.stage
                    or task["expires_at"] <= now or task["state"] not in {"pending", "accepted", "outcome_unknown"}):
                raise AdmissionRefused("browser_task_unavailable")
            next_generation = str(uuid4())
            # Reattachment changes presentation only, never the accepted operation or deadline.
            connection.execute("UPDATE desktop_browser_tasks SET desktop_generation=?, admission_generation=?, expires_at=? WHERE task_id=?",
                               (generation, next_generation, min(task["expires_at"], parent["expires_at"]), task_id))
            connection.execute("DELETE FROM desktop_admission_entries WHERE role='browser_task' AND reference_id=?", (task_id,))
            connection.execute("DELETE FROM desktop_admission_sessions WHERE role='browser_task' AND reference_id=?", (task_id,))
            if not browser_entry:
                return next_generation
            return self._entry(connection, "browser_task", task_id, next_generation,
                               min(task["expires_at"], parent["expires_at"]), now)

    def prepare_accepted_task(self, operation_id: str, *, purpose: str, deadline: float) -> str:
        """Continue a durable coordinator command, without a renderer credential.

        This internal port admits only declared command/purpose pairs. Replaying
        it reads the original task and cannot change its scope or deadline.
        """
        require_journey(operation_id)
        allowed = {"browser.access": {"browser.access"},
                   "account.connect": {"browser.authentication"},
                   "activation.review": {"browser.authentication"},
                   "auth.select": {"browser.authentication"},
                   "auth.switch": {"browser.authentication"},
                   "browser.pair": {"browser.pairing-confirmation"}}
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise AdmissionRefused("task_scope_invalid")
        with self.authentication.database.transaction() as connection:
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if (operation is None or purpose not in allowed.get(operation["action"], set())
                    or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}):
                raise AdmissionRefused("task_scope_invalid")
            existing = connection.execute("SELECT * FROM desktop_browser_tasks WHERE operation_id=?", (operation_id,)).fetchone()
            if existing is not None:
                if existing["purpose"] != purpose:
                    raise AdmissionRefused("task_scope_invalid")
                return existing["task_id"]
            parent = connection.execute("SELECT * FROM desktop_presentations WHERE generation=?", (operation["generation"],)).fetchone()
            now = self._now()
            if parent is None or parent["journey_id"] != operation["journey_id"]:
                raise AdmissionRefused("task_scope_changed")
            expires = min(now + 1800, parent["expires_at"], operation["expires_at"], deadline)
            if expires <= now:
                raise AdmissionRefused("task_expired")
            self._capacity(connection)
            task_id, admission_generation = str(uuid4()), str(uuid4())
            connection.execute("UPDATE desktop_browser_tasks SET state='cancelled' WHERE journey_id=? AND purpose=? "
                "AND operation_id IN (SELECT operation_id FROM desktop_setup_operations WHERE state IN ('completed','refused'))",
                (operation["journey_id"], purpose))
            connection.execute("INSERT INTO desktop_browser_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, operation["journey_id"], operation["generation"], operation_id, purpose,
                 operation["scope_digest"], operation["source_revision"], admission_generation, self.stage, expires))
            # The current native owner obtains a single-use entry through the
            # existing reissue port. No bearer is saved or replayed as a receipt.
            return task_id

    def task(self, identifier: str, *, generation: str) -> dict:
        with self.authentication.database.read() as connection:
            _, task = self._require(connection, identifier, role="browser_task", generation=generation)
            return {name: task[name] for name in ("task_id", "journey_id", "operation_id", "purpose", "scope_digest",
                                                 "source_revision", "admission_generation", "state", "expires_at")}

    @staticmethod
    def _capacity(connection):
        count = connection.execute("SELECT COUNT(*) FROM desktop_admission_entries WHERE role='browser_task'").fetchone()[0]
        unresolved = connection.execute("SELECT COUNT(*) FROM desktop_browser_tasks WHERE state IN ('pending', 'accepted', 'outcome_unknown')").fetchone()[0]
        if count >= MAX_ADMISSIONS or unresolved >= MAX_ADMISSIONS:
            raise AdmissionRefused("admission_limit_reached")

    @staticmethod
    def _cleanup(connection, now):
        connection.execute("DELETE FROM desktop_authentication_closures WHERE expires_at<=?", (now,))
        connection.execute("DELETE FROM desktop_admission_entries WHERE expires_at<=?", (now,))
        connection.execute("DELETE FROM desktop_admission_sessions WHERE expires_at<=?", (now,))
        connection.execute("UPDATE desktop_presentations SET state='retired' WHERE expires_at<=?", (now,))
        connection.execute("UPDATE desktop_browser_tasks SET state='expired' WHERE expires_at<=? AND state='pending'", (now,))
        # These are presentation records, not the coordinator's durable operation
        # evidence. Terminal records have no purpose beyond the original task
        # deadline. Accepted/unknown work remains available for reconciliation.
        terminal = "SELECT task_id FROM desktop_browser_tasks WHERE expires_at<=? AND state IN ('completed', 'cancelled', 'expired')"
        for table in ("desktop_admission_entries", "desktop_admission_sessions"):
            connection.execute(f"DELETE FROM {table} WHERE role='browser_task' AND reference_id IN ({terminal})", (now,))
        connection.execute("DELETE FROM desktop_browser_tasks WHERE task_id IN (" + terminal + ")", (now,))
        connection.execute("DELETE FROM desktop_presentations WHERE state='retired' "
            "AND generation NOT IN (SELECT refresh_source FROM desktop_presentations WHERE refresh_source IS NOT NULL) "
            "AND generation NOT IN (SELECT desktop_generation FROM desktop_browser_tasks) "
            "AND generation NOT IN (SELECT generation FROM desktop_setup_operations WHERE state IN ('accepted','waiting','outcome_unknown'))")
