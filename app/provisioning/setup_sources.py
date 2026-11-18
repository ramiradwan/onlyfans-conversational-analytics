"""Typed source lifecycle views over the existing provisioning evidence owners."""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.onboarding.contract import digest
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.persistence.saved_workspace import SavedWorkspaceStore


@dataclass(frozen=True)
class SourceBinding:
    creator_account_id: str | None
    association_request_id: str | None
    organization_id: str | None
    installation_key_id: str | None
    installation_key_jkt: str | None


@dataclass(frozen=True)
class InitialPreparation:
    profile: str | None
    request_digest: str
    installation_key_id: str | None
    public_key_jwk: str | None


@dataclass(frozen=True)
class RecordedTerminalRead:
    ceremony_id: str
    binding_digest: str
    domain_status: Literal["unavailable"]


@dataclass(frozen=True)
class RecordedRegisteredTerminalRead:
    source_sha256: str
    operation_id: str
    observed_at: datetime
    actor_digest: str
    result: Literal["expired"] = "expired"


@dataclass(frozen=True)
class SetupSourceResolution:
    journey_id: str
    kind: Literal["initial-enrollment", "registered-continuation"]
    state: str
    revision: int | None
    installation_id: str
    operation_id: str | None
    expires_at: datetime
    recovery_deadline: datetime | None
    provenance: Literal["current", "recovery", "retired"]
    binding: SourceBinding | None
    preparation: InitialPreparation | None
    handoff_reference: str | None
    handoff_expires_at: str | None
    terminal_read: RecordedTerminalRead | RecordedRegisteredTerminalRead | None


@dataclass(frozen=True)
class SourceRecoveryProposal:
    source_journey_id: str
    classification_generation: str
    creator_account_id: str
    association_request_id: str | None
    initial: bool = False

    def scope(self):
        # Preserve the exact current command-fencing bytes and domain owner API.
        value = {"source_journey_id": self.source_journey_id,
            "classification_generation": self.classification_generation,
            "creator_account_id": self.creator_account_id,
            "association_request_id": self.association_request_id}
        if self.initial:
            value["initial"] = True
        return value


class SetupSourceLifecycle:
    def __init__(self, store, journeys, continuations):
        self.store, self.journeys, self.continuations = store, journeys, continuations

    def effective(self, source, *, connection=None):
        if source is None:
            return None
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            row = current.execute("SELECT r.*,j.operation_id AS resolved_operation,j.installation_id AS resolved_installation,"
                "j.recovery_deadline FROM onboarding_workspace_recovery r JOIN onboarding_journeys j "
                "ON j.journey_id=r.resolved_journey_id WHERE r.previous_journey_id=?", (source,)).fetchone()
        if row is None:
            return source
        if (row["state"] not in {"preparing", "prepare-unknown", "unknown", "completing", "completed"}
                or row["operation_id"] != row["resolved_operation"]
                or row["installation_id"] != row["resolved_installation"]
                or row["expires_at"] != row["recovery_deadline"]):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return row["resolved_journey_id"]

    def current(self, source, *, connection=None):
        if source is None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            # Keep the owner's live-authority validation in the caller's transaction.
            row = self.journeys.require_current(source, connection=current)
            detail = current.execute("SELECT scope_json FROM installation_continuation_contexts WHERE journey_id=?", (source,)).fetchone()
            return self._typed(row, None if detail is None else json.loads(detail["scope_json"]), "current")

    def resolve(self, source, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            source = self.effective(source, connection=current)
            row = current.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (source,)).fetchone()
            detail = current.execute("SELECT scope_json FROM installation_continuation_contexts WHERE journey_id=?", (source,)).fetchone()
            retired = current.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=? AND expires_at>?",
                (source, self.store._now().timestamp())).fetchone()
            evidence = None if retired is None else json.loads(retired["evidence_json"])
            proof = self._terminal_read(evidence, source)
            if row is not None:
                return self._typed(dict(row), None if detail is None else json.loads(detail["scope_json"]), "current", terminal_read=proof)
            recovery = current.execute("SELECT * FROM onboarding_workspace_recovery WHERE previous_journey_id=? AND expires_at>?",
                (source, self.store._now().isoformat())).fetchone()
        if recovery is not None:
            return self._typed({**dict(recovery), "journey_id": source, "kind": "initial-enrollment",
                "expires_at": recovery["retired_at"], "recovery_deadline": recovery["expires_at"]}, None, "recovery", terminal_read=proof)
        if retired is None:
            return None
        row = evidence.get("journey")
        if row is None:
            return None
        if row["journey_id"] != source:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        scope = None if not evidence.get("context") else json.loads(evidence["context"]["scope_json"])
        return self._typed(row, scope, "retired", terminal_read=proof)

    @staticmethod
    def _terminal_read(evidence, source):
        if evidence is None or evidence.get("terminal_read") is None:
            return None
        row, marker = evidence.get("journey"), evidence["terminal_read"]
        if row is None or row["journey_id"] != source:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        # These are the two distinct records written by the proof owners. A
        # generic unavailable response or elapsed clock never creates evidence.
        if row["kind"] == "initial-enrollment" and marker.get("domain_status") == "unavailable":
            return RecordedTerminalRead(marker["ceremony_id"], marker["binding_digest"], "unavailable")
        if row["kind"] == "registered-continuation" and marker.get("profile") == "runtime-terminal-read/v1":
            from app.persistence.installation_continuation import CONTINUATION_PROFILE
            if marker["response"] == {"profile": CONTINUATION_PROFILE, "operation_id": row["operation_id"], "result": "expired"}:
                return RecordedRegisteredTerminalRead(marker["source_sha256"], row["operation_id"],
                    datetime.fromisoformat(marker["observed_at"]), marker["actor_digest"])
        raise OnboardingJourneyUnavailable("journey_unavailable")

    @staticmethod
    def _typed(row, registered_scope, provenance, *, terminal_read=None):
        scope = registered_scope or json.loads(row.get("scope_json") or "{}")
        binding = None if not scope else SourceBinding(
            scope.get("creator_account_id", scope.get("intended_creator_id")), scope.get("association_request_id"),
            scope.get("organization_id"), scope.get("installation_key_id"), scope.get("installation_key_jkt"))
        request = None if row.get("prepare_json") is None else json.loads(row["prepare_json"])
        key = {} if request is None else request.get("destination", {}).get("installation_key", {})
        preparation = None if request is None else InitialPreparation(request.get("profile"), digest(request),
            key.get("kid"), None if key.get("jwk") is None else json.dumps(key["jwk"], sort_keys=True))
        return SetupSourceResolution(row["journey_id"], row["kind"], row["state"], row.get("revision"),
            row["installation_id"], row.get("operation_id"), datetime.fromisoformat(row["expires_at"]),
            None if row.get("recovery_deadline") is None else datetime.fromisoformat(row["recovery_deadline"]),
            provenance, binding, preparation, row.get("handoff_reference"), row.get("handoff_expires_at"), terminal_read)

    def recovery_proposal(self, connection, source, *, work=(), initial_creator=None):
        if initial_creator is not None:
            from app.persistence.source_preparations import SourcePreparations
            if SourcePreparations(self.store).classify(source, connection=connection) != "never-prepared":
                raise OnboardingJourneyUnavailable("workspace_changed")
        value = (SavedWorkspaceStore(self.continuations).inspect_initial(connection, source, initial_creator, work=work)
            if initial_creator is not None else self.continuations.desktop_recovery_proposal(connection, journey_id=source, work=work))
        return SourceRecoveryProposal(value["source_journey_id"], value["classification_generation"],
            value["creator_account_id"], value["association_request_id"], value.get("initial", False))


class PreparedSourceRecovery:
    """Exact source recovery under the existing provisioning selection owners.

    The caller must journal the explicit customer action and provide its live
    scope/authority guard and atomic desktop-source adoption callback. This
    adapter never opens Chrome, accepts an account, or grants authority.
    """
    def __init__(self, sources, clients):
        from app.persistence.source_replacement_reservations import SourceReplacementReservations
        self.sources, self.store, self.clients = sources, sources.store, clients
        self.reservations = SourceReplacementReservations(self.store)

    def proposal(self, connection, source, creator, *, work=()):
        prior = self.sources.resolve(source, connection=connection)
        if prior is None or prior.journey_id != source or prior.state != "expired":
            raise OnboardingJourneyUnavailable("workspace_changed")
        if prior.kind not in self.clients:
            raise OnboardingJourneyUnavailable("workspace_changed")
        if prior.kind == "initial-enrollment":
            base = SavedWorkspaceStore(self.sources.continuations).inspect_initial(connection, source, creator, work=work)
        else:
            base = self.sources.continuations._desktop_terminal_proposal(connection, journey_id=source, work=work)
        if base["creator_account_id"] != creator:
            raise OnboardingJourneyUnavailable("workspace_changed")
        coordinates = self.reservations.describe(connection, journey_id=source, creator_account_id=creator,
            authorize=lambda **_: None)
        return {**base, "replacement": coordinates}

    @staticmethod
    def _arguments(proposal, authorize):
        coords = proposal["replacement"]
        return {"operation_id": coords["operation_id"], "expected_digest": coords["expected_digest"],
                "creator_account_id": proposal["creator_account_id"], "authorize": authorize}

    def reserve(self, connection, proposal, *, authorize, work=()):
        def guard(*, connection):
            authorize(connection=connection)
            if self.proposal(connection, proposal["source_journey_id"], proposal["creator_account_id"], work=work) != proposal:
                raise OnboardingJourneyUnavailable("workspace_changed")
        return self.reservations.reserve(connection, **self._arguments(proposal, guard))

    def execute(self, proposal, *, successor_journey_id, authorize, adopt, reconcile, work=lambda: ()):
        from app.security.hosted_grants import HostedGrantUnavailable
        from app.security.source_replacement import SourceReplacementRefused
        coords = proposal["replacement"]
        original = coords["operation_id"]

        def guard(*, connection):
            authorize(connection=connection)
            current = self.proposal(connection, proposal["source_journey_id"], proposal["creator_account_id"], work=work())
            # Presentation cleanup may change the classification generation
            # after the explicit command was journaled. The exact original
            # source, selection and every eligibility check must still agree.
            if ({name: value for name, value in current.items() if name != "classification_generation"}
                    != {name: value for name, value in proposal.items() if name != "classification_generation"}):
                raise OnboardingJourneyUnavailable("workspace_changed")

        arguments = self._arguments(proposal, guard)
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            reservation = connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
                "WHERE predecessor_operation_id=?", (original,)).fetchone()
            if reservation is None or reservation["creator_account_id"] != proposal["creator_account_id"]:
                raise OnboardingJourneyUnavailable("workspace_changed")
            existing = self.reservations.outcome(connection, original)
            if existing is not None:
                if existing["status"] == "prepared" and existing["successor_journey_id"] != successor_journey_id:
                    raise OnboardingJourneyUnavailable("workspace_changed")
                return existing
            if reconcile:
                # A reserved but unsent request is not restarted implicitly.
                if reservation["state"] != "send-claimed":
                    return None
                request = self.reservations.claim_reconciliation(connection, **arguments)
            else:
                request = self.reservations.claim_send(connection, **arguments)
        if request is None:
            return None

        def before_send():
            with self.store.database.read() as connection:
                if self.reservations.require_claimed(connection, **arguments) != request:
                    raise OnboardingJourneyUnavailable("workspace_changed")

        client = self.clients[coords["kind"]]
        try:
            result = client.replace(request, before_send=before_send,
                intended_creator_id=proposal["creator_account_id"])
        except SourceReplacementRefused as refusal:
            if refusal.reason != "commercial_reconciliation_required":
                return None
            with self.store.database.transaction() as connection:
                return self.reservations.record_commercial_refusal(connection, **arguments, refusal=refusal)
        except HostedGrantUnavailable:
            return None
        with self.store.database.transaction() as connection:
            outcome = self.reservations.record_result(connection, **arguments,
                successor_journey_id=successor_journey_id, result=result)
            if outcome["status"] == "prepared":
                adopt(connection=connection, successor_journey_id=successor_journey_id)
            return outcome
