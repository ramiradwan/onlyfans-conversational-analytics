"""Durable, exact local intent for continuing an enrolled installation."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4

from app.persistence.continuation_selection import (
    ContinuationSelectionStore, ContinuationSelectionUnavailable, ContinuationTarget,
)
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable, _digest, require_journey
from app.persistence.auth import (
    AuthenticationStateError, VerifiedGrantReference,
)
from app.persistence.catchup_events import capture_authority_change


REGISTERED_CONTINUATION = "registered-continuation"
INITIAL_ENROLLMENT = "initial-enrollment"
CONTINUATION_PROFILE = "urn:bridge-clean:installation-setup-continuation:v2"
_STATES = {"prepare-unknown", "waiting", "completing", "unknown", "completed", "expired", "revoked", "reauthentication-required"}
_PROVIDER_STATES = {"awaiting-owner", "awaiting-approval", "approved", "authentication-required", "expired", "revoked"}


def _require_transition(previous: str, state: str) -> None:
    if previous == "new" or (previous in {"expired", "revoked"} and state != previous):
        raise OnboardingJourneyUnavailable("continuation_changed")
    if previous == "completed" and state not in {"completed", "expired", "revoked"}:
        raise OnboardingJourneyUnavailable("continuation_changed")


def _json(value) -> str:
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError("Unsupported saved selection value")
    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"))


def _generation(target: ContinuationTarget) -> str:
    candidate = asdict(target.candidate)
    # Approval and finalization may advance for this same account. They are not
    # permission to change the original claim, key or selected association.
    candidate.pop("state")
    candidate.pop("resolved_at")
    return _json({"claim": asdict(target.claim), "key": asdict(target.key), "candidate": candidate})


def _scope(target: ContinuationTarget) -> dict:
    return {
        "organization_id": target.claim.organization_id,
        "installation_id": target.claim.installation_id,
        "installation_key_id": target.key.installation_key_id,
        "source_onboarding_transaction_id": target.claim.onboarding_transaction_id,
        "creator_account_id": target.candidate.creator_account_id,
        "association_request_id": target.candidate.association_request_id,
    }


class InstallationContinuationStore:
    def __init__(self, authentication) -> None:
        self.authentication = authentication
        self.journeys = OnboardingJourneyStore(authentication)
        self.selection = ContinuationSelectionStore(authentication)

    def _saved_native_scope(self, connection, previous_journey_id: str | None) -> dict | None:
        """Navigation history may constrain a native proposal, never authorize it."""
        moment = self.authentication._now()
        now = moment.isoformat()
        protected = "('preparing','prepare-unknown','completing','unknown')"
        initial = connection.execute("SELECT expires_at,recovery_deadline FROM onboarding_journeys WHERE kind=? AND state IN "
            + protected, (INITIAL_ENROLLMENT,)).fetchall()
        # Use the same original fixed deadline as journey cleanup, without
        # deleting uncertainty before the admission decision has examined it.
        if (any((datetime.fromisoformat(row["recovery_deadline"]) if row["recovery_deadline"]
                else datetime.fromisoformat(row["expires_at"]) + timedelta(minutes=30)) > moment for row in initial)
                or connection.execute("SELECT 1 FROM onboarding_workspace_recovery WHERE expires_at>? AND state IN "
                    + protected + " LIMIT 1", (now,)).fetchone()
                or connection.execute("SELECT 1 FROM onboarding_uncertain_receipts WHERE expires_at>? LIMIT 1",
                    (now,)).fetchone()):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if previous_journey_id is None:
            return None
        previous = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?",
            (previous_journey_id,)).fetchone()
        if previous is not None and previous["kind"] == REGISTERED_CONTINUATION:
            detail = connection.execute("SELECT scope_json FROM installation_continuation_contexts WHERE journey_id=?",
                (previous_journey_id,)).fetchone()
            if detail is None:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return json.loads(detail["scope_json"])
        if previous is not None and previous["expires_at"] > now:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if previous is None:
            previous = connection.execute("SELECT * FROM onboarding_workspace_recovery WHERE previous_journey_id=?",
                (previous_journey_id,)).fetchone()
        if previous is None:
            return None
        scope = {} if previous["scope_json"] is None else json.loads(previous["scope_json"])
        if previous["installation_id"] is not None:
            if scope.get("installation_id", previous["installation_id"]) != previous["installation_id"]:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            scope["installation_id"] = previous["installation_id"]
        return scope

    def saved_native_target(self, previous_journey_id: str | None) -> ContinuationTarget:
        with self.authentication.database.transaction(immediate=False) as connection:
            scope = self._saved_native_scope(connection, previous_journey_id)
            association = None if scope is None else scope.get("association_request_id")
            if association is not None:
                target = self.selection.select_in_transaction(connection, association_request_id=association)
            else:
                target = self.select_for_admission()
            self.require_saved_native_target(connection, target, previous_journey_id)
            return target

    def require_saved_native_target(self, connection, target: ContinuationTarget,
                                    previous_journey_id: str | None) -> None:
        scope = self._saved_native_scope(connection, previous_journey_id)
        expected = {**_scope(target), "intended_creator_id": target.candidate.creator_account_id,
                    "onboarding_transaction_id": target.claim.onboarding_transaction_id}
        if scope is not None and any(key in scope and scope[key] != value for key, value in expected.items()):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        self.selection.require_current(connection, target)

    def select_for_admission(self, journey_id: str | None = None) -> ContinuationTarget:
        """Retain an admitted live target before making an automatic proposal."""
        with self.authentication.database.transaction(immediate=False) as connection:
            if journey_id is None:
                rows = connection.execute(
                    "SELECT journey_id FROM onboarding_journeys WHERE kind=? AND expires_at>? LIMIT 2",
                    (REGISTERED_CONTINUATION, self.authentication._now().isoformat()),
                ).fetchall()
                if len(rows) > 1:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                if rows:
                    journey_id = rows[0]["journey_id"]
            if journey_id is not None:
                target = self.require_current(journey_id, connection=connection)["target"]
            else:
                target = self.selection.select_in_transaction(connection)
            self._saved_native_scope(connection, None)
            return target

    def admit_native_session(
        self, *, target: ContinuationTarget, reopened_key, identifier: str, csrf: str,
        entry_identifier: str, entry_id: str, entry_expires_at: float,
        existing_identifier: str | None, ttl_seconds: float, authorize,
        previous_journey_id: str | None = None,
        saved_navigation: bool = False,
    ) -> dict:
        """Commit fresh native authority, an exact saved target and its session."""
        if reopened_key != target.key:
            raise ContinuationSelectionUnavailable("selection_changed")
        with self.authentication.database.transaction() as connection:
            authorize()
            now = self.authentication._now()
            if entry_expires_at <= now.timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")
            self._saved_native_scope(connection, None)
            if saved_navigation:
                self.require_saved_native_target(connection, target, previous_journey_id)
            self.journeys._cleanup(connection, now)
            self.selection.require_current(connection, target)
            receipt = connection.execute(
                "SELECT * FROM onboarding_native_selection_receipts WHERE entry_digest=?",
                (_digest(entry_identifier),),
            ).fetchone()
            if receipt is not None:
                raise OnboardingJourneyUnavailable("native_entry_used")
            if connection.execute("SELECT count(*) FROM onboarding_native_selection_receipts").fetchone()[0] >= 128:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            existing = None if not existing_identifier else connection.execute(
                "SELECT s.*, j.kind, j.expires_at AS journey_expires_at FROM provisioning_browser_sessions s "
                "JOIN onboarding_journeys j ON j.journey_id=s.journey_id WHERE s.session_digest=?",
                (_digest(existing_identifier),),
            ).fetchone()
            if existing is not None and existing["expires_at"] > now.timestamp():
                # A competing valid session must be reconciled by its owner;
                # this fresh-selection transaction cannot retarget it.
                raise OnboardingJourneyUnavailable("journey_unavailable")
            active = connection.execute(
                "SELECT j.*, c.selection_json FROM onboarding_journeys j "
                "JOIN installation_continuation_contexts c ON c.journey_id=j.journey_id "
                "WHERE j.expires_at>?", (now.isoformat(),),
            ).fetchall()
            if len(active) > 1 or (active and active[0]["selection_json"] != _generation(target)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            if active:
                journey_id = active[0]["journey_id"]
                deadline = datetime.fromisoformat(active[0]["expires_at"])
            else:
                if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                journey_id = str(uuid4())
                deadline = now + timedelta(seconds=min(ttl_seconds, 1800))
                connection.execute(
                    "INSERT INTO onboarding_journeys (journey_id,created_at,expires_at,installation_id,state,operation_id,kind) "
                    "VALUES (?,?,?,?,'new',?,?)",
                    (journey_id, now.isoformat(), deadline.isoformat(), target.claim.installation_id,
                     self.journeys.new_operation(), REGISTERED_CONTINUATION),
                )
                connection.execute(
                    "INSERT INTO installation_continuation_contexts (journey_id,operation_profile,association_request_id,selection_json,scope_json) "
                    "VALUES (?,?,?,?,?)",
                    (journey_id, CONTINUATION_PROFILE, target.candidate.association_request_id, _generation(target), _json(_scope(target))),
                )
            expires_at = min(now.timestamp() + ttl_seconds, deadline.timestamp())
            self.journeys.record_session(
                identifier=identifier, csrf=csrf, journey_id=journey_id, expires_at=expires_at,
                authorize=authorize, connection=connection,
            )
            connection.execute(
                "INSERT INTO onboarding_native_selection_receipts VALUES (?,?,?,?,?,?)",
                (_digest(entry_identifier), entry_id, journey_id, _digest(identifier),
                 min(entry_expires_at, expires_at), previous_journey_id),
            )
            authorize()
            self.selection.require_current(connection, target)
            self._saved_native_scope(connection, None)
            if saved_navigation:
                self.require_saved_native_target(connection, target, previous_journey_id)
            if entry_expires_at <= self.authentication._now().timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")
            return {"identifier": identifier, "journey_id": journey_id, "expires_at": expires_at}

    def retain_native_session(self, *, journey_id: str, identifier: str, entry_identifier: str,
                              entry_id: str, entry_expires_at: float, authorize,
                              previous_journey_id: str | None = None, saved_target: ContinuationTarget | None = None) -> None:
        """Record the same live session's result without issuing new authority."""
        with self.authentication.database.transaction() as connection:
            authorize()
            now = self.authentication._now()
            if saved_target is not None:
                self.require_saved_native_target(connection, saved_target, previous_journey_id)
            self.journeys._cleanup(connection, now)
            current = self.require_current(journey_id, connection=connection)
            if saved_target is not None and current["target"] != saved_target:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            session = connection.execute(
                "SELECT * FROM provisioning_browser_sessions WHERE session_digest=? AND journey_id=?",
                (_digest(identifier), journey_id),
            ).fetchone()
            if session is None:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            deadline = min(entry_expires_at, session["expires_at"],
                           datetime.fromisoformat(current["expires_at"]).timestamp())
            if deadline <= now.timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")
            if connection.execute("SELECT count(*) FROM onboarding_native_selection_receipts").fetchone()[0] >= 128:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            if connection.execute("SELECT 1 FROM onboarding_native_selection_receipts WHERE entry_digest=?",
                                  (_digest(entry_identifier),)).fetchone() is not None:
                raise OnboardingJourneyUnavailable("native_entry_used")
            connection.execute("INSERT INTO onboarding_native_selection_receipts VALUES (?,?,?,?,?,?)",
                (_digest(entry_identifier), entry_id, journey_id, _digest(identifier), deadline, previous_journey_id))
            authorize()
            if saved_target is not None:
                self.require_saved_native_target(connection, saved_target, previous_journey_id)
            if deadline <= self.authentication._now().timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")

    def native_receipt(self, entry_identifier: str) -> dict | None:
        with self.authentication.database.read() as connection:
            row = connection.execute(
                "SELECT r.*, s.expires_at AS session_expires_at, j.expires_at AS journey_expires_at "
                "FROM onboarding_native_selection_receipts r "
                "JOIN provisioning_browser_sessions s ON s.session_digest=r.session_digest AND s.journey_id=r.journey_id "
                "JOIN onboarding_journeys j ON j.journey_id=r.journey_id "
                "WHERE r.entry_digest=? AND j.kind=?",
                (_digest(entry_identifier), REGISTERED_CONTINUATION),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        now = self.authentication._now().timestamp()
        if min(value["expires_at"], value["session_expires_at"],
               datetime.fromisoformat(value["journey_expires_at"]).timestamp()) <= now:
            return None
        return value

    def require_current(self, journey_id: str, *, connection=None) -> dict:
        if connection is None:
            with self.authentication.database.transaction(immediate=False) as current:
                return self.require_current(journey_id, connection=current)
        row = connection.execute(
            "SELECT j.*, c.operation_profile, c.association_request_id, c.selection_json, c.scope_json AS continuation_scope_json, "
            "c.continuation_id, c.reference, c.provider_expires_at, c.epoch, c.provider_revision, c.provider_state "
            "FROM onboarding_journeys j JOIN installation_continuation_contexts c ON c.journey_id=j.journey_id "
            "WHERE j.journey_id=? AND j.kind=?", (require_journey(journey_id), REGISTERED_CONTINUATION),
        ).fetchone()
        if row is None or row["operation_profile"] != CONTINUATION_PROFILE:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if datetime.fromisoformat(row["expires_at"]) <= self.authentication._now():
            raise OnboardingJourneyUnavailable("journey_expired")
        target = self.selection.select_in_transaction(connection, association_request_id=row["association_request_id"])
        if row["selection_json"] != _generation(target) or row["continuation_scope_json"] != _json(_scope(target)):
            raise ContinuationSelectionUnavailable("selection_changed")
        result = dict(row)
        result["scope"] = json.loads(result.pop("continuation_scope_json"))
        result["target"] = target
        return result

    def begin_prepare(self, journey_id: str) -> dict | None:
        """Claim the single send before any external call; crashes read only."""
        with self.authentication.database.transaction() as connection:
            row = self.require_current(journey_id, connection=connection)
            if row["state"] != "new":
                return None
            connection.execute(
                "UPDATE onboarding_journeys SET state='prepare-unknown',revision=revision+1 WHERE journey_id=?",
                (journey_id,),
            )
            return self.require_current(journey_id, connection=connection)

    def bind_result(self, journey_id: str, *, continuation_id: str, expires_at: str,
                    reference: str | None = None, epoch: str | None = None,
                    revision: int | None = None, provider_state: str | None = None,
                    state: str = "waiting") -> dict:
        """Pin the provider identity once; later snapshots retain its epoch."""
        if state not in _STATES or (epoch is None) != (revision is None):
            raise ValueError("Invalid continuation result")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("Invalid continuation revision")
        if (epoch is None and provider_state is not None) or (epoch is not None and provider_state not in _PROVIDER_STATES):
            raise ValueError("Invalid provider state")
        if provider_state in {"expired", "revoked"} and state != provider_state:
            raise ValueError("Invalid terminal state")
        provider_deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if provider_deadline.tzinfo is None:
            raise ValueError("Invalid continuation deadline")
        with self.authentication.database.transaction() as connection:
            row = self.require_current(journey_id, connection=connection)
            if row["state"] == "new":
                raise OnboardingJourneyUnavailable("preparation_required")
            if row["continuation_id"] is not None and (
                row["continuation_id"] != continuation_id
                or datetime.fromisoformat(row["provider_expires_at"].replace("Z", "+00:00")) != provider_deadline
                or (row["reference"] is not None and reference is not None and row["reference"] != reference)
            ):
                raise OnboardingJourneyUnavailable("continuation_changed")
            if row["epoch"] is not None and epoch != row["epoch"]:
                raise OnboardingJourneyUnavailable("continuation_changed")
            if (row["provider_revision"] is not None and revision is not None
                    and revision < row["provider_revision"]):
                raise OnboardingJourneyUnavailable("continuation_changed")
            if row["provider_state"] in {"expired", "revoked"} and provider_state != row["provider_state"]:
                raise OnboardingJourneyUnavailable("continuation_changed")
            if row["provider_revision"] is not None and revision == row["provider_revision"]:
                if provider_state != row["provider_state"]:
                    raise OnboardingJourneyUnavailable("continuation_changed")
                # Replayed projection cannot change local completion or wake a
                # new action. Read reconciliation returns the retained state.
                return row
            _require_transition(row["state"], state)
            connection.execute(
                "UPDATE installation_continuation_contexts SET continuation_id=?,provider_expires_at=?,"
                "reference=COALESCE(reference,?),epoch=COALESCE(epoch,?),provider_revision=COALESCE(?,provider_revision),"
                "provider_state=COALESCE(?,provider_state) WHERE journey_id=?",
                (continuation_id, expires_at, reference, epoch, revision, provider_state, journey_id),
            )
            connection.execute(
                "UPDATE onboarding_journeys SET state=?,reason=?,revision=revision+1 WHERE journey_id=?",
                (state, "authentication-required" if provider_state == "authentication-required" else None, journey_id),
            )
            return self.require_current(journey_id, connection=connection)

    def mark_state(self, journey_id: str, state: str) -> dict:
        if state not in _STATES:
            raise ValueError("Invalid continuation state")
        with self.authentication.database.transaction() as connection:
            row = self.require_current(journey_id, connection=connection)
            _require_transition(row["state"], state)
            connection.execute("UPDATE onboarding_journeys SET state=?,revision=revision+1 WHERE journey_id=?", (state, journey_id))
            return self.require_current(journey_id, connection=connection)

    @capture_authority_change
    def complete_binding(self, journey_id: str, grant: VerifiedGrantReference, *, membership_reference_id: str) -> dict:
        """Commit a verified binding and this exact context's completion together."""
        with self.authentication.database.transaction() as connection:
            current = self.require_current(journey_id, connection=connection)
            target = current["target"]
            now = self.authentication._now()
            if (current["provider_state"] != "approved"
                    or current["state"] not in {"waiting", "completing", "unknown", "completed"}
                    or current["provider_expires_at"] is None
                    or datetime.fromisoformat(current["provider_expires_at"].replace("Z", "+00:00")) <= now):
                raise AuthenticationStateError("Continuation binding is unavailable")
            self.authentication.admit_continuation_binding_in_transaction(
                connection, grant, membership_reference_id=membership_reference_id,
                claim=target.claim, candidate=target.candidate, key=target.key,
                binding_revoked_at=target.binding_revoked_at,
            )
            connection.execute("UPDATE onboarding_journeys SET state='completed',revision=revision+1 WHERE journey_id=?",
                               (journey_id,))
            return self.require_current(journey_id, connection=connection)
