"""Local ownership and retry rules for TEST text-analysis acquisition."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import RLock
from uuid import uuid4

from app.persistence.auth import _new_uuid7
from app.persistence.text_analysis_native_acquisition import (
    AcquisitionJournalError, AcquisitionRecord, TextAnalysisAcquisitionStore,
)
from app.security.runtime_policy import require_role
from app.security.text_analysis_acquisition import (
    AcquisitionContext, NativeConsent, PAID_TERMS, _schema, acquisition_confirmation, build_issuance_request,
    build_prepare_request, build_read_request, build_recovery_request, build_submit_request,
    validate_acquisition_response, validate_acquisition_selection,
)
from app.security.text_analysis_acquisition_transport import (
    NativeAcquisitionClient, NativeAcquisitionNotCompleted, NativeAcquisitionTransport, acquisition_entry_url,
)
from app.security.text_analysis_contract import OUTPUT_POLICY, PREFIX, TextAnalysisSchemas, canonical, parse_json
from app.security.text_analysis_identity import TextAnalysisIdentityOwner

_BINDING = ("organization_id", "installation_id", "installation_key_id", "installation_key_jkt")
_STATES = {
    "awaiting-payment": "awaiting_browser", "awaiting-authentication": "awaiting_browser",
    "confirmation-required": "confirmation_required", "authorized": "authorized",
    "expired": "expired", "cancelled": "cancelled", "unavailable": "unavailable",
}


@dataclass(frozen=True)
class AcquisitionSelection:
    """Independently selected installed release and eligible licence or preview seat."""
    release_identity: bytes
    expected_scope: bytes
    supported_profiles: tuple[str, ...]


def _need(condition, reason="acquisition_unavailable"):
    if not condition:
        raise AcquisitionJournalError(reason)


def _time(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _authority(kind):
    return "license" if kind == "paid" else "preview"


class TextAnalysisNativeAcquisition:
    """Compose only with the explicitly selected TEST release and issuer policies."""

    def __init__(self, authentication, client, authorities, *, select, eligible, payment_mode, clock=None):
        _need(payment_mode == "test", "acquisition_test_only")
        self.authentication = authentication
        self.identity = TextAnalysisIdentityOwner(authentication)
        self.store = TextAnalysisAcquisitionStore(authentication)
        self.client = client
        self.authorities = authorities
        self.select = select
        self.eligible = eligible
        self.origin = client.transport.origin
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fence = authentication.database.dependency_authority_gate
        self.actions = RLock()

    def _identity(self, policy, kind):
        _need(kind in ("paid", "preview"))
        require_role(policy, "creator")
        identity = self.identity.capture(policy)
        binding = {key: getattr(identity, key) for key in _BINDING}
        scope = sha256(canonical([self.origin, identity.principal_id, identity.creator_account_id, binding])).hexdigest()
        key = self.authentication.installation_key_reference()
        _need(key is not None and all(getattr(key, name) == binding[name] for name in _BINDING[2:]))
        _need(self.eligible(policy, kind, binding.copy()))
        return scope, binding, key

    def _selection(self, policy, kind, binding, *, refresh=False):
        selection = self.select(policy, kind, binding.copy(), refresh=refresh)
        _need(type(selection) is AcquisitionSelection)
        release = parse_json(selection.release_identity, maximum=1024, require_canonical=True)
        _need(release.get("platform") == "windows")
        validate_acquisition_selection(kind, selection.expected_scope, selection.release_identity,
                                       selection.supported_profiles, schemas=self.client.schemas)
        return selection

    def _owned(self, policy, kind, record, current=None, *, reopen=False):
        scope, binding, key = current or self._identity(policy, kind)
        selection = self._selection(policy, kind, binding)
        _need(record is not None and record.kind == kind and record.scope == scope)
        _need(record.data["prepare"]["binding"] == binding
              and record.data["prepare"]["release_identity"] == parse_json(selection.release_identity, maximum=1024)
              and tuple(record.data["prepare"]["supported_profiles"]["profiles"]) == selection.supported_profiles
              and canonical(record.data["expected_scope"]) == selection.expected_scope
              and record.data["key"] == [key.provider_name, key.provider_key_name, key.public_key_jwk])
        _need(self.store.get(record.flow_id) == record, "acquisition_changed")
        if reopen:
            _need(self.client.key_authority.reopen_existing() == key, "acquisition_key_changed")
        return binding, key

    def _context(self, record, *, observed_at=None):
        deadline = record.data.get("operation_expires_at")
        if deadline is None:
            upper = observed_at or self.clock()
            deadline = _time(upper + timedelta(seconds=1800))
        return AcquisitionContext(canonical(record.data["prepare"]), canonical(record.data["expected_scope"]),
                                  deadline, kind=record.kind)

    def _restore(self, record, name, previous=None):
        observed = record.data.get(name)
        if observed is None:
            return None
        return validate_acquisition_response(observed["body"].encode("utf-8"), operation=observed["operation"],
            context=self._context(record), schemas=self.client.schemas, server_now=_parse(observed["at"]),
            previous=previous)

    def _chain(self, record):
        prepared = self._restore(record, "entry")
        reviewed = self._restore(record, "decision", prepared)
        authorized = self._restore(record, "authorized", reviewed or prepared)
        latest = self._restore(record, "latest", authorized or reviewed or prepared)
        return prepared, reviewed, authorized, latest

    def _project(self, record):
        prepared, reviewed, authorized, latest = self._chain(record)
        projection = latest or authorized or reviewed or prepared
        now = self.clock()
        state = "prepared" if projection is None else _STATES[projection.state]
        expiry = projection.expires_at if projection is not None else record.data.get("operation_expires_at")
        pending = record.data.get("submit_pending", False)
        if record.data.get("adopted"):
            state = "adopted"
        elif authorized is not None:
            state = ("expired" if not record.data.get("issuance") and _parse(authorized.expires_at) <= now
                     else "authorized")
        elif pending and state in ("prepared", "awaiting_browser", "unavailable", "confirmation_required"):
            state = "confirming"
        elif expiry and _parse(expiry) <= now and state in ("awaiting_browser", "confirmation_required"):
            state = "expired" if not pending else "confirming"
        review = None
        if state == "confirmation_required" and reviewed and _parse(reviewed.expires_at) > now:
            terms = {"execution_limit": 1}
            if record.kind == "paid":
                terms = {key: PAID_TERMS[key] for key in
                         ("payment_mode", "amount_minor", "currency", "licensed_major_version", "update_rights")}
            review = {"review_id": record.data["review_id"], "terms": terms, "expires_at": reviewed.expires_at}
        return {"flow_id": record.flow_id, "state": state, "expires_at": expiry, "review": review,
                "navigation_available": state == "awaiting_browser" and prepared is not None
                and prepared.entry_reference is not None and _parse(prepared.expires_at) > now}

    def status(self, policy, kind):
        """Read only; capability ownership here includes a redeemed preview."""
        with self.fence:
            if not self.client.schemas.has_schema(_schema(kind, "prepare-response")):
                return {"eligible": False, "reason": "disabled", "current": None}
            try:
                current = self._identity(policy, kind)
                record = self.store.latest(current[0], kind)
                if record is not None:
                    self._owned(policy, kind, record, current)
                acquired = bool(record and record.data.get("adopted"))
                if not acquired:
                    acquired = self.authorities.read_current(policy, kind=_authority(kind)) is not None
            except ValueError:
                return {"eligible": False, "reason": "account_unavailable", "current": None}
            return {"eligible": not acquired, "reason": "already_acquired" if acquired else None,
                    "current": None if record is None else self._project(record)}

    def _explicit_selection(self, policy, kind, flow_id=None):
        _need(self.client.schemas.has_schema(_schema(kind, "prepare-response")), "acquisition_disabled")
        with self.fence:
            scope, binding, key = self._identity(policy, kind)
            if flow_id is not None:
                record = self.store.get(flow_id)
                _need(record is not None and record.kind == kind and record.scope == scope)
        # Fetch only on explicit action and outside the account/DB fence. Recheck
        # the live binding and exact verified release before any native mutation.
        selection = self._selection(policy, kind, binding, refresh=True)
        with self.fence:
            _need(self._identity(policy, kind) == (scope, binding, key))
            _need(self._selection(policy, kind, binding) == selection)
        return scope, binding, selection, key

    def _exchange(self, policy, record, operation, body):
        def allowed():
            with self.fence:
                self._owned(policy, record.kind, record, reopen=True)
        with self.fence:
            binding, key = self._owned(policy, record.kind, record, reopen=True)
        return self.client.exchange(operation, body, binding, key, before_send=allowed)

    def _observe(self, policy, record, operation, raw):
        with self.fence:
            self._owned(policy, record.kind, record)
            now = self.clock()
            prepared, review, authorized, last = self._chain(record)
            seen = validate_acquisition_response(raw, operation=operation, schemas=self.client.schemas,
                context=self._context(record, observed_at=now), server_now=now,
                previous=last or authorized or review or prepared)
            receipt = {"operation": operation, "at": _time(now), "body": raw.decode("utf-8")}
            changes = {"latest": receipt}
            if seen.expires_at and not record.data.get("operation_expires_at"):
                changes["operation_expires_at"] = seen.expires_at
            if seen.entry_reference is not None:
                changes["entry"] = receipt
            if seen.state == "confirmation-required":
                if review is None:
                    changes.update(decision=receipt, review_id=str(uuid4()))
                if record.data.get("submit") is not None:
                    # A fresh read still shows this exact decision. A new explicit
                    # click may retry it, never an automatic submit.
                    _need(operation != "prepare")
                    changes["submit_pending"] = False
            if seen.state == "authorized":
                _need(record.data.get("submit") is not None and review is not None
                      and seen.confirmation_digest == review.confirmation_digest, "acquisition_consent_missing")
                changes["authorized"] = receipt
            return self.store.update(record, **changes)

    def prepare(self, policy, kind):
        with self.actions:
            scope, binding, selection, key = self._explicit_selection(policy, kind)
            with self.fence:
                _need(self._identity(policy, kind) == (scope, binding, key))
                _need(self.authorities.read_current(policy, kind=_authority(kind)) is None, "acquisition_already_owned")
                record = self.store.latest(scope, kind)
                replace_record = record is None
                if record is not None:
                    self._owned(policy, kind, record)
                    current = self._project(record)
                    replace_record = current["state"] in ("expired", "cancelled") and "submit" not in record.data
                    if not replace_record and (current["navigation_available"]
                            or current["state"] not in ("prepared", "awaiting_browser")):
                        return current
                if replace_record:
                    operation_id = _new_uuid7(self.clock())
                    prepare = {"profile": PREFIX + kind + "-acquisition:v1", "operation_id": operation_id,
                               "binding": binding, "authority_profile": PREFIX + _authority(kind) + ":v1",
                               "supported_profiles": {"profile": PREFIX + "supported-profiles:v1",
                                                       "profiles": list(selection.supported_profiles)},
                               "release_identity": parse_json(selection.release_identity, maximum=1024)}
                    if kind == "paid":
                        prepare["offer_id"] = PAID_TERMS["offer_id"]
                    data = {"key": [key.provider_name, key.provider_key_name, key.public_key_jwk], "prepare": prepare,
                            "expected_scope": parse_json(selection.expected_scope, maximum=4096)}
                    new = AcquisitionRecord(str(uuid4()), scope, kind, operation_id, 1, _time(self.clock()), data)
                    build_prepare_request(self._context(new), schemas=self.client.schemas)
                    record = self.store.begin(new, previous=record)
            body = build_prepare_request(self._context(record), schemas=self.client.schemas)
            raw = self._exchange(policy, record, kind + "-prepare", body)
            return self._project(self._observe(policy, record, "prepare", raw))

    def read(self, policy, kind, flow_id):
        with self.actions:
            self._explicit_selection(policy, kind, flow_id)
            with self.fence:
                record = self.store.get(flow_id)
                self._owned(policy, kind, record)
                if record.data.get("adopted"):
                    return self._project(record)
                local_authorized = record.data.get("authorized") is not None
            if not local_authorized:
                body = build_read_request(self._context(record), schemas=self.client.schemas)
                raw = self._exchange(policy, record, kind + "-read", body)
                record = self._observe(policy, record, "read", raw)
            return self._finish(policy, record)

    def submit(self, policy, kind, flow_id, review_id):
        with self.actions:
            with self.fence:
                record = self.store.get(flow_id)
                self._owned(policy, kind, record)
                shown = self._project(record)["review"]
                _need(shown is not None and review_id == shown["review_id"], "acquisition_review_changed")
                _, decision, _, _ = self._chain(record)
                consent = NativeConsent(record.operation_id, decision.confirmation_digest, decision.decision_digest,
                    decision.decision_reference, "activate-paid-text-analysis" if kind == "paid" else "acquire-text-analysis-preview")
                body = build_submit_request(decision, native_consent=consent, schemas=self.client.schemas, server_now=self.clock())
                original = record.data.get("submit")
                _need(original is None or original.encode("utf-8") == body, "acquisition_review_changed")
                record = self.store.update(record, submit=body.decode("utf-8"), submit_pending=True)
            raw = self._exchange(policy, record, kind + "-submit", body)
            return self._finish(policy, self._observe(policy, record, "submit", raw))

    def _finish(self, policy, record):
        with self.fence:
            self._owned(policy, record.kind, record)
            _, _, authorized, _ = self._chain(record)
            if authorized is None or record.data.get("adopted"):
                return self._project(record)
            authority = _authority(record.kind)
            confirmation = acquisition_confirmation(authorized, schemas=self.client.schemas)
            retained = record.data.get("issuance")
            if retained is not None:
                request = retained.encode("utf-8")
            else:
                if _parse(confirmation.context.expires_at) <= self.clock():
                    return self._project(record)
                request = build_issuance_request(confirmation, authority_kind=authority,
                    schemas=self.client.schemas, server_now=self.clock())
                record = self.store.update(record, issuance=request.decode("utf-8"))
        if retained is not None:
            recover = build_recovery_request(request, schemas=self.client.schemas)
            try:
                delivery = self._exchange(policy, record, "authority-recover", recover)
            except NativeAcquisitionNotCompleted:
                _need(_parse(confirmation.context.expires_at) > self.clock(), "acquisition_issuance_expired")
                delivery = self._exchange(policy, record, authority + "-issue", request)
        else:
            delivery = self._exchange(policy, record, authority + "-issue", request)
        with self.fence:
            self._owned(policy, record.kind, record)
            self.authorities.adopt(policy, issued_from=confirmation, original_issuance_request=request, delivery=delivery)
            return self._project(self.store.update(record, adopted=True, entry=None, decision=None,
                latest=None, authorized=None, review_id=None, submit=None, submit_pending=False))

    def navigation(self, policy, kind, flow_id):
        with self.fence:
            record = self.store.get(flow_id)
            self._owned(policy, kind, record)
            prepared, _, _, _ = self._chain(record)
            _need(self._project(record)["navigation_available"], "acquisition_navigation_unavailable")
            return acquisition_entry_url(self.origin, kind, prepared.entry_reference)


_service = None


def compose_installed_text_analysis_acquisition(authentication, key_authority, authorities, *,
        origin, payment_mode, eligible, supported_profiles, clock=None):
    """Bind the installed owner's verified basis and adopter to the native transport.

    The package-selected pilot supplies eligibility, HTTPS origin and downstream
    profiles. Construction and eligibility perform no release fetch.
    """
    _need(payment_mode == "test", "acquisition_test_only")
    profiles = {kind: tuple(values) for kind, values in supported_profiles.items()}
    _need(profiles and set(profiles) <= {"preview", "paid"})
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    client = NativeAcquisitionClient(NativeAcquisitionTransport(origin), key_authority,
                                     TextAnalysisSchemas(), clock=selected_clock)

    def select(policy, kind, _binding, *, refresh):
        release, seat = authorities.acquisition_basis(policy, refresh=refresh)
        scope = {"seat_id": seat, "seat_scope": "organization-installation-seat",
                 "capability": "text-analysis", "compatible_artifact_family": "text-analysis-artifact",
                 "licensed_major_version": 1, "update_rights": True, "fallback_major_versions": []}
        if kind == "preview":
            scope.update(execution_limit=1, output_policy=OUTPUT_POLICY)
        return AcquisitionSelection(release, canonical(scope), profiles[kind])

    def allowed(policy, kind, binding):
        return kind in profiles and eligible(policy, kind, binding) is True

    return TextAnalysisNativeAcquisition(authentication, client, authorities, select=select,
        eligible=allowed, payment_mode=payment_mode, clock=selected_clock)


def configure_native_text_analysis_acquisition(service):
    global _service
    if service is not None and type(service) is not TextAnalysisNativeAcquisition:
        raise TypeError("native_acquisition_service_required")
    _service = service


def native_text_analysis_acquisition():
    return _service
