"""Durable, exact local intent for continuing an enrolled installation."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4

from app.persistence.continuation_selection import (
    ContinuationSelectionStore, ContinuationSelectionUnavailable, ContinuationTarget,
)
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable, _digest, require_journey
from app.persistence.auth import (
    AuthenticationStateError, VerifiedGrantReference, _require_uuid7,
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

    @staticmethod
    def _runtime_terminal_source(evidence, target, now):
        if (not isinstance(evidence, dict) or set(evidence) - {"journey", "context", "unresolved_work", "terminal_read"}
                or evidence.get("unresolved_work") != ["journey-operation"]):
            return None
        journey, detail = evidence.get("journey"), evidence.get("context")
        if (not isinstance(journey, dict) or not isinstance(detail, dict)
                or set(journey) != {"journey_id", "kind", "state", "operation_id", "installation_id", "expires_at", "scope_json"}
                or set(detail) != {"selection_json", "scope_json", "continuation_id", "provider_expires_at", "provider_state"}
                or journey["kind"] != REGISTERED_CONTINUATION or journey["state"] != "waiting"
                or journey["installation_id"] != target.claim.installation_id
                or detail["provider_state"] != "awaiting-owner" or detail["selection_json"] != _generation(target)
                or detail["scope_json"] != _json(_scope(target)) or journey["scope_json"] is not None
                or not isinstance(detail["continuation_id"], str) or not 0 < len(detail["continuation_id"]) <= 128):
            return None
        try:
            require_journey(journey["journey_id"])
            _require_uuid7(journey["operation_id"], name="operation_id")
            local = datetime.fromisoformat(journey["expires_at"])
            provider = datetime.fromisoformat(detail["provider_expires_at"].replace("Z", "+00:00"))
            if local.tzinfo is None or provider.tzinfo is None or max(local, provider) > now:
                return None
        except (AttributeError, TypeError, ValueError):
            return None
        original = {name: evidence[name] for name in ("journey", "context", "unresolved_work")}
        return {"journey_id": journey["journey_id"], "operation_id": journey["operation_id"],
            "continuation_id": detail["continuation_id"], "scope": _scope(target),
            "source_sha256": hashlib.sha256(_json(original).encode()).hexdigest()}

    @classmethod
    def _runtime_terminal_closed(cls, evidence, target, now):
        source = cls._runtime_terminal_source(evidence, target, now)
        marker = evidence.get("terminal_read") if isinstance(evidence, dict) else None
        if (source is None or not isinstance(marker, dict)
                or set(marker) != {"profile", "source_sha256", "response", "observed_at", "actor_digest"}
                or marker["profile"] != "runtime-terminal-read/v1" or marker["source_sha256"] != source["source_sha256"]
                or marker["response"] != {"profile": CONTINUATION_PROFILE, "operation_id": source["operation_id"], "result": "expired"}
                or not isinstance(marker["actor_digest"], str) or len(marker["actor_digest"]) != 64
                or any(char not in "0123456789abcdef" for char in marker["actor_digest"])):
            return False
        try:
            observed = datetime.fromisoformat(marker["observed_at"])
            return observed.tzinfo is not None and max(datetime.fromisoformat(evidence["journey"]["expires_at"]),
                datetime.fromisoformat(evidence["context"]["provider_expires_at"].replace("Z", "+00:00"))) <= observed <= now
        except (AttributeError, TypeError, ValueError):
            return False

    def runtime_terminal_read_scope(self, *, previous_journey_id, creator_account_id, authorize, connection=None):
        """Read an exact closed-time waiting context without renewing its authority."""
        if connection is None:
            with self.authentication.database.transaction(immediate=False) as current:
                return self.runtime_terminal_read_scope(previous_journey_id=previous_journey_id,
                    creator_account_id=creator_account_id, authorize=authorize, connection=current)
        require_journey(previous_journey_id); authorize()
        now = self.authentication._now()
        retired = connection.execute("SELECT * FROM onboarding_presentation_retirements WHERE journey_id=?",
            (previous_journey_id,)).fetchone()
        if retired is None:
            return None
        target = self.selection.select_in_transaction(connection)
        if (not target.finalized or target.binding_revoked_at is not None or target.candidate.state.value != "approved"
                or target.candidate.creator_account_id != creator_account_id):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        evidence = json.loads(retired["evidence_json"])
        source = self._runtime_terminal_source(evidence, target, now)
        if source is None or self._runtime_terminal_closed(evidence, target, now):
            return None
        if "terminal_read" in evidence or source["journey_id"] != previous_journey_id or retired["expires_at"] <= now.timestamp():
            raise OnboardingJourneyUnavailable("journey_unavailable")
        self._saved_native_scope(connection, None)
        if connection.execute("SELECT 1 FROM onboarding_journeys WHERE journey_id=? OR state IN "
                "('preparing','prepare-unknown','completing','unknown') LIMIT 1", (previous_journey_id,)).fetchone() is not None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if connection.execute("SELECT 1 FROM installation_continuation_contexts LIMIT 1").fetchone() is not None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        for table, column in (("onboarding_transfer_intents", "journey_id"), ("onboarding_transfer_relays", "journey_id"),
                ("onboarding_transfer_proofs", "journey_id"), ("onboarding_workspace_recovery", "previous_journey_id")):
            if connection.execute("SELECT 1 FROM " + table + " WHERE " + column + "=? LIMIT 1",
                    (previous_journey_id,)).fetchone() is not None:
                raise OnboardingJourneyUnavailable("journey_unavailable")
        histories = connection.execute("SELECT journey_id,evidence_json FROM onboarding_presentation_retirements LIMIT 129").fetchall()
        if len(histories) > 128:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        for history in histories:
            other = json.loads(history["evidence_json"])
            if history["journey_id"] != previous_journey_id and other.get("unresolved_work") and not self._runtime_terminal_closed(other, target, now):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        authorize(); self.selection.require_current(connection, target)
        return {**source, "retained_until": retired["expires_at"], "selection_json": _generation(target)}

    def commit_runtime_terminal_closure(self, *, source, creator_account_id, actor_digest, response, authorize):
        """Record only a verified terminal read, retaining the original history."""
        if (response != {"profile": CONTINUATION_PROFILE, "operation_id": source["operation_id"], "result": "expired"}
                or not isinstance(actor_digest, str) or len(actor_digest) != 64
                or any(char not in "0123456789abcdef" for char in actor_digest)):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        with self.authentication.database.transaction() as connection:
            current = self.runtime_terminal_read_scope(previous_journey_id=source["journey_id"],
                creator_account_id=creator_account_id, authorize=authorize, connection=connection)
            if current != source:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            row = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
                (source["journey_id"],)).fetchone()
            evidence = json.loads(row["evidence_json"])
            evidence["terminal_read"] = {"profile": "runtime-terminal-read/v1", "source_sha256": source["source_sha256"],
                "response": response, "observed_at": self.authentication._now().isoformat(), "actor_digest": actor_digest}
            authorize()
            connection.execute("UPDATE onboarding_presentation_retirements SET evidence_json=? WHERE journey_id=?",
                (_json(evidence), source["journey_id"]))

    def runtime_activation_context(self, *, previous_journey_id: str, creator_account_id: str,
                                   authorize, reopened_key=None, create: bool = False, connection=None) -> dict | None:
        """Keep one exact registered operation for a passkey-authenticated account.

        Runtime authority does not issue a provisioning browser session or repeat
        installation admission. Expired coordinates constrain the saved target;
        they never extend an old operation's deadline.
        """
        require_journey(previous_journey_id)
        with (self.authentication.database.transaction() if connection is None else nullcontext(connection)) as connection:
            authorize()
            now = self.authentication._now()
            target = self.selection.select_in_transaction(connection)
            if (not target.finalized or target.binding_revoked_at is not None
                    or target.candidate.state.value != "approved"
                    or target.candidate.creator_account_id != creator_account_id
                    or (create and reopened_key != target.key)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            self._saved_native_scope(connection, None)
            previous = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?",
                (previous_journey_id,)).fetchone()
            previous_context = connection.execute("SELECT * FROM installation_continuation_contexts WHERE journey_id=?",
                (previous_journey_id,)).fetchone()
            scope = None
            empty_shadow = (previous is not None and previous_context is None
                and previous["kind"] == INITIAL_ENROLLMENT and previous["state"] == "new"
                and all(previous[name] is None for name in ("operation_id", "scope_json", "prepare_json",
                    "handoff_reference", "handoff_expires_at", "reason", "recovery_deadline"))
                and previous["revision"] == 0)
            if empty_shadow:
                # A display read from an older client may have recreated an
                # empty draft. It supplies no scope; only exact retired evidence
                # can correlate the authenticated account to its former owner.
                for table, column in (("provisioning_browser_sessions", "journey_id"),
                        ("onboarding_native_selection_receipts", "journey_id"),
                        ("onboarding_transfer_intents", "journey_id"), ("onboarding_transfer_relays", "journey_id"),
                        ("onboarding_transfer_proofs", "journey_id"), ("onboarding_workspace_recovery", "previous_journey_id")):
                    if connection.execute("SELECT 1 FROM " + table + " WHERE " + column + "=? LIMIT 1",
                            (previous_journey_id,)).fetchone() is not None:
                        raise OnboardingJourneyUnavailable("journey_unavailable")
                scope = self._runtime_retired_scope(connection, previous_journey_id, target, empty_shadow=True)
            elif previous is not None:
                if previous["installation_id"] != target.claim.installation_id:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                if previous["kind"] == REGISTERED_CONTINUATION and previous_context is not None:
                    if previous_context["selection_json"] != _generation(target):
                        raise OnboardingJourneyUnavailable("journey_unavailable")
                    scope = json.loads(previous_context["scope_json"])
                    if (previous["state"] in {"prepare-unknown", "completing", "unknown"}
                            and previous_context["provider_expires_at"] is None
                            and datetime.fromisoformat(previous["expires_at"]) <= now):
                        raise OnboardingJourneyUnavailable("journey_unavailable")
                elif previous["kind"] == INITIAL_ENROLLMENT and previous["state"] == "completed":
                    scope = json.loads(previous["scope_json"] or "{}")
                else:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
            else:
                scope = self._runtime_retired_scope(connection, previous_journey_id, target)
            expected = {**_scope(target), "intended_creator_id": creator_account_id,
                "onboarding_transaction_id": target.claim.onboarding_transaction_id,
                "installation_key_jkt": target.key.installation_key_jkt}
            if scope is None or not scope or any(name in scope and scope[name] != value for name, value in expected.items()):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            required = {"organization_id", "installation_id", "association_request_id"}
            if not required <= set(scope) or not ("creator_account_id" in scope or "intended_creator_id" in scope):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return self._runtime_context(connection, target, now, create=create, authorize=authorize)

    def runtime_desktop_context(self, *, creator_account_id, reopened_key, authorize, connection=None):
        """Prepare an installed account from explicit authenticated desktop intent.

        Durable account provenance supplies the exact installation target; an
        expired browser presentation is neither required nor renewed. Existing
        uncertain outcomes still fence new source operations.
        """
        with (self.authentication.database.transaction() if connection is None else nullcontext(connection)) as connection:
            authorize()
            target = self.selection.select_in_transaction(connection)
            if (not target.finalized or target.binding_revoked_at is not None
                    or target.candidate.state.value != "approved"
                    or target.candidate.creator_account_id != creator_account_id
                    or reopened_key != target.key):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return self._runtime_context(connection, target, self.authentication._now(), create=True, authorize=authorize)

    def _runtime_context(self, connection, target, now, *, create, authorize):
        for retired in connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements"):
            evidence = json.loads(retired["evidence_json"])
            detail = evidence.get("context")
            if (detail is not None and evidence.get("unresolved_work") and detail["selection_json"] == _generation(target)
                    and not self._runtime_terminal_closed(evidence, target, now)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        # An expired waiting context still needs its terminal read. A local
        # cleanup may retain that exact history, but cannot resolve it.
        for waiting in connection.execute("SELECT j.expires_at,c.provider_expires_at FROM onboarding_journeys j "
                "JOIN installation_continuation_contexts c ON c.journey_id=j.journey_id "
                "WHERE j.state='waiting' AND c.provider_state='awaiting-owner' AND c.selection_json=?",
                (_generation(target),)):
            if (datetime.fromisoformat(waiting["expires_at"]) <= now or
                    (waiting["provider_expires_at"] is not None and datetime.fromisoformat(
                        waiting["provider_expires_at"].replace("Z", "+00:00")) <= now)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        if connection.execute("SELECT 1 FROM onboarding_journeys j JOIN installation_continuation_contexts c "
                "ON c.journey_id=j.journey_id WHERE j.expires_at<=? AND c.selection_json=? "
                "AND j.state IN ('prepare-unknown','completing','unknown') AND c.provider_expires_at IS NULL LIMIT 1",
                (now.isoformat(), _generation(target))).fetchone() is not None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        active = connection.execute("SELECT j.*,c.selection_json,c.provider_expires_at FROM onboarding_journeys j "
            "JOIN installation_continuation_contexts c ON c.journey_id=j.journey_id "
            "WHERE j.expires_at>? AND j.state NOT IN ('expired','revoked')", (now.isoformat(),)).fetchall()
        live = []
        for row in active:
            if row["selection_json"] != _generation(target):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            if row["provider_expires_at"] is not None and datetime.fromisoformat(
                    row["provider_expires_at"].replace("Z", "+00:00")) <= now:
                # A known expired Provider context cannot accept another
                # protected action. Keep its evidence and retire its use.
                if create:
                    connection.execute("UPDATE onboarding_journeys SET state='expired',revision=revision+1 WHERE journey_id=?",
                        (row["journey_id"],))
                continue
            live.append(row)
        if len(live) > 1:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if live:
            result = self.require_current(live[0]["journey_id"], connection=connection)
        elif not create:
            result = None
        else:
            self.journeys._cleanup(connection, now)
            if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            journey_id = str(uuid4())
            connection.execute("INSERT INTO onboarding_journeys "
                "(journey_id,created_at,expires_at,installation_id,state,operation_id,kind) VALUES (?,?,?,?,'new',?,?)",
                (journey_id, now.isoformat(), (now+timedelta(minutes=30)).isoformat(),
                 target.claim.installation_id, self.journeys.new_operation(), REGISTERED_CONTINUATION))
            connection.execute("INSERT INTO installation_continuation_contexts "
                "(journey_id,operation_profile,association_request_id,selection_json,scope_json) VALUES (?,?,?,?,?)",
                (journey_id, CONTINUATION_PROFILE, target.candidate.association_request_id,
                 _generation(target), _json(_scope(target))))
            result = self.require_current(journey_id, connection=connection)
        authorize()
        self.selection.require_current(connection, target)
        return result

    def _runtime_retired_scope(self, connection, journey_id, target, *, empty_shadow=False):
        retired = connection.execute("SELECT evidence_json,expires_at FROM onboarding_presentation_retirements WHERE journey_id=?",
            (journey_id,)).fetchone()
        if retired is None:
            return None
        if retired["expires_at"] <= self.authentication._now().timestamp():
            raise OnboardingJourneyUnavailable("journey_expired")
        evidence = json.loads(retired["evidence_json"])
        if evidence.get("unresolved_work") and not self._runtime_terminal_closed(evidence, target, self.authentication._now()):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        detail, journey = evidence.get("context"), evidence.get("journey")
        if journey is None or journey["journey_id"] != journey_id or journey["installation_id"] != target.claim.installation_id:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if detail is not None:
            if (journey["kind"] != REGISTERED_CONTINUATION or detail["selection_json"] != _generation(target)
                    or (empty_shadow and (journey["state"] not in {"completed", "expired"}
                        or detail["provider_state"] != "approved" or detail["scope_json"] != _json(_scope(target))))):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return json.loads(detail["scope_json"])
        if not empty_shadow and journey["kind"] == INITIAL_ENROLLMENT and journey["state"] == "completed":
            return json.loads(journey["scope_json"] or "{}")
        return None

    def runtime_activation_receipt(self, *, previous_journey_id: str, journey_id: str,
                                   actor_digest: str, authorize, create: bool = False,
                                   receipt_id: str | None = None) -> dict | None:
        """Bind a nonauthorizing workspace transition to the unchanged local actor."""
        require_journey(previous_journey_id); require_journey(journey_id)
        if receipt_id is not None:
            require_journey(receipt_id)
        with self.authentication.database.transaction() as connection:
            authorize()
            now = self.authentication._now()
            row = self.require_current(journey_id, connection=connection)
            if row["state"] in {"expired", "revoked"}:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            deadline = datetime.fromisoformat(row["expires_at"]).timestamp()
            if row["provider_expires_at"] is not None:
                deadline = min(deadline, datetime.fromisoformat(row["provider_expires_at"].replace("Z", "+00:00")).timestamp())
            if deadline <= now.timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")
            connection.execute("DELETE FROM runtime_activation_workspace_receipts WHERE expires_at<=?", (now.timestamp(),))
            receipt = connection.execute("SELECT * FROM runtime_activation_workspace_receipts "
                "WHERE previous_journey_id=? AND journey_id=? AND actor_digest=?",
                (previous_journey_id, journey_id, actor_digest)).fetchone()
            if receipt is None and create and previous_journey_id != journey_id:
                if connection.execute("SELECT count(*) FROM runtime_activation_workspace_receipts").fetchone()[0] >= 128:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                identifier = str(uuid4())
                connection.execute("INSERT INTO runtime_activation_workspace_receipts VALUES (?,?,?,?,?,?)",
                    (identifier, previous_journey_id, journey_id, actor_digest, row["selection_json"],
                     min(now.timestamp()+300, deadline)))
                receipt = connection.execute("SELECT * FROM runtime_activation_workspace_receipts WHERE receipt_id=?",
                    (identifier,)).fetchone()
            if receipt is None:
                return None
            if (receipt["selection_json"] != row["selection_json"]
                    or (receipt_id is not None and receipt["receipt_id"] != receipt_id)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            authorize()
            self.selection.require_current(connection, row["target"])
            return {"receipt_id": receipt["receipt_id"], "previous_journey_id": previous_journey_id,
                "journey_id": journey_id, "expires_at": int(min(receipt["expires_at"], deadline)*1000)}

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

    def desktop_recovery_proposal(self, connection, *, journey_id, work=()):
        """Classify an expired exact source without renewing it or starting work."""
        from app.persistence.saved_workspace import SavedWorkspaceStore
        require_journey(journey_id)
        previous = connection.execute("SELECT expires_at,state FROM onboarding_journeys WHERE journey_id=?",
            (journey_id,)).fetchone()
        if (previous is not None and previous["state"] not in {"expired", "revoked"}
                and datetime.fromisoformat(previous["expires_at"]) > self.authentication._now()):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        target = self.selection.select_in_transaction(connection)
        self.require_saved_native_target(connection, target, journey_id)
        result = SavedWorkspaceStore(self).inspect(target, journey_id, journey_id, connection=connection, work=work)
        if result["classification"] not in {"inactive", "same_target"}:
            raise OnboardingJourneyUnavailable("workspace_changed")
        evidence = result["evidence"]
        prior = evidence.get("journey")
        detail = evidence.get("context")
        scope = None if prior is None else json.loads((detail or {}).get("scope_json") or prior.get("scope_json") or "{}")
        expected = {**_scope(target), "intended_creator_id": target.candidate.creator_account_id,
            "onboarding_transaction_id": target.claim.onboarding_transaction_id}
        if prior is not None and (prior["installation_id"] != target.claim.installation_id
                or scope and any(name in scope and scope[name] != value for name, value in expected.items())):
            raise OnboardingJourneyUnavailable("workspace_changed")
        return {"source_journey_id": journey_id, "classification_generation": result["classification_generation"],
            "creator_account_id": target.candidate.creator_account_id,
            "association_request_id": target.candidate.association_request_id}

    def admit_desktop_recovery(self, connection, *, proposal, reopen_key, work=()):
        from app.persistence.saved_workspace import SavedWorkspaceStore
        current = self.desktop_recovery_proposal(connection, journey_id=proposal["source_journey_id"], work=work)
        if current != proposal:
            raise OnboardingJourneyUnavailable("workspace_changed")
        target = self.selection.select_in_transaction(connection)
        SavedWorkspaceStore(self).require_selection(connection, target, proposal["source_journey_id"],
            proposal["source_journey_id"], proposal["classification_generation"], work=work)
        if reopen_key() != target.key:
            raise ContinuationSelectionUnavailable("selection_changed")
        self.selection.require_current(connection, target)
        return self._new_desktop_context(connection, target, self.authentication._now())

    def admit_initial_desktop_recovery(self, connection, *, proposal, work=()):
        from app.persistence.saved_workspace import SavedWorkspaceStore
        if SavedWorkspaceStore(self).inspect_initial(connection, proposal["source_journey_id"],
                proposal["creator_account_id"], work=work) != proposal:
            raise OnboardingJourneyUnavailable("workspace_changed")
        self.journeys._require_initial_native_candidate(connection)
        # An exact terminal source is retained; only explicit admission creates
        # a new source. Its unused presentation lifetime must not select it again.
        return self.journeys._open(connection, str(uuid4()), self.authentication._now())["journey_id"]

    def admit_desktop_context(self, connection, *, journey_id: str | None, reopen_key) -> str:
        """Select a domain context without creating a browser or operator session.

        The desktop coordinator calls this inside its command-admission write
        transaction. A locator retains an exact context; it cannot retarget one.
        """
        now = self.authentication._now()
        if journey_id is not None:
            row = connection.execute("SELECT kind,expires_at FROM onboarding_journeys WHERE journey_id=?",
                                     (require_journey(journey_id),)).fetchone()
            if row is not None:
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    raise OnboardingJourneyUnavailable("journey_expired")
                if row["kind"] == INITIAL_ENROLLMENT:
                    return journey_id
                target = self.require_current(journey_id, connection=connection)["target"]
                if reopen_key() != target.key:
                    raise ContinuationSelectionUnavailable("selection_changed")
                self.selection.require_current(connection, target)
                return journey_id
            self.journeys._require_initial_native_candidate(connection)
            return self.journeys._open(connection, journey_id, now)["journey_id"]
        # Existing initial work is retained before proposing any saved target.
        rows = connection.execute("SELECT journey_id FROM onboarding_journeys WHERE expires_at>? LIMIT 2",
                                  (now.isoformat(),)).fetchall()
        if len(rows) > 1:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if rows:
            return self.admit_desktop_context(connection, journey_id=rows[0]["journey_id"], reopen_key=reopen_key)
        try:
            target = self.selection.select_in_transaction(connection)
        except ContinuationSelectionUnavailable as error:
            if error.reason != "registration_required":
                raise
            self.journeys._require_initial_native_candidate(connection)
            return self.journeys._open(connection, None, now)["journey_id"]
        if reopen_key() != target.key:
            raise ContinuationSelectionUnavailable("selection_changed")
        self._saved_native_scope(connection, None)
        if connection.execute("SELECT 1 FROM onboarding_uncertain_receipts WHERE expires_at>?", (now.isoformat(),)).fetchone():
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return self._new_desktop_context(connection, target, now)

    def _new_desktop_context(self, connection, target, now):
        if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        journey_id = str(uuid4())
        connection.execute(
            "INSERT INTO onboarding_journeys (journey_id,created_at,expires_at,installation_id,state,operation_id,kind) "
            "VALUES (?,?,?,?,'new',?,?)", (journey_id, now.isoformat(), (now + timedelta(minutes=30)).isoformat(),
                target.claim.installation_id, self.journeys.new_operation(), REGISTERED_CONTINUATION))
        connection.execute(
            "INSERT INTO installation_continuation_contexts (journey_id,operation_profile,association_request_id,selection_json,scope_json) "
            "VALUES (?,?,?,?,?)", (journey_id, CONTINUATION_PROFILE, target.candidate.association_request_id,
                _generation(target), _json(_scope(target))))
        self.selection.require_current(connection, target)
        return journey_id

    def admit_native_session(
        self, *, target: ContinuationTarget, reopened_key, identifier: str, csrf: str,
        entry_identifier: str, entry_id: str, entry_expires_at: float,
        existing_identifier: str | None, ttl_seconds: float, authorize,
        previous_journey_id: str | None = None,
        saved_navigation: bool = False,
        supersession: dict | None = None,
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
            reconciliation = None
            if supersession is not None:
                from app.persistence.saved_workspace import SavedWorkspaceStore
                reconciliation = SavedWorkspaceStore(self).require_selection(connection, target,
                    supersession["record_journey_id"], supersession["candidate_journey_id"],
                    supersession["classification_generation"], work=supersession.get("work", ()),
                    retained_identifier=existing_identifier)
            elif saved_navigation:
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
            if existing is not None and existing["expires_at"] > now.timestamp() and supersession is None:
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
            if supersession is not None:
                if existing is not None and existing["expires_at"] > now.timestamp() and existing["journey_id"] != journey_id:
                    raise OnboardingJourneyUnavailable("workspace_changed")
                # The installation context and operation survive; only old
                # browser authority is retired before publishing its successor.
                connection.execute("DELETE FROM provisioning_browser_sessions WHERE journey_id=?", (journey_id,))
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
            if supersession is not None:
                from app.persistence.saved_workspace import canonical
                connection.execute("INSERT INTO onboarding_presentation_sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    (_digest(identifier), entry_id, entry_id, supersession["record_journey_id"],
                     supersession["candidate_journey_id"], reconciliation["classification"],
                     reconciliation["classification_generation"], canonical(reconciliation["evidence"]), expires_at))
            authorize()
            self.selection.require_current(connection, target)
            self._saved_native_scope(connection, None)
            if saved_navigation and supersession is None:
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
