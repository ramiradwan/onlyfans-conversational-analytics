"""Explicit original-operation licence acquisition with durable local ownership."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from threading import RLock

from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.persistence.text_analysis_reissue import TextAnalysisReissueStore, ReissueJournalError, scope_digest
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import RuntimePolicy, RuntimeAuthorizationDenied, require_role
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
from app.security.text_analysis_contract import canonical
from app.security.text_analysis_reissue import (
    ReissueContext, ValidatedReissueResult, TextAnalysisReissueError,
    validate_context, validate_confirmation, validate_reissue_projection,
    validate_reissue_result, build_read_request, build_recover_request, build_finalize_request,
)
from app.security.text_analysis_reissue_transport import browser_entry_url


@dataclass(frozen=True)
class ReissueStatus:
    flow_id: str
    state: str
    navigation_available: bool


def _need(ok, code='reissue_context_unavailable'):
    if not ok:
        raise ReissueJournalError(code)


class TextAnalysisReissueService:
    """No network work occurs during construction, discovery or status reads.

    Trusted composition supplies the original release selection only to begin.
    Resume always reconstructs that selection from its retained canonical body.
    """
    def __init__(self, authentication, client, *, provider_origin, context_factory,
                 authority_context_factory,
                 clock=lambda: datetime.now(timezone.utc)):
        self.authentication, self.client = authentication, client
        self.store = TextAnalysisReissueStore(authentication, clock=clock)
        self.authorities = TextAnalysisAuthorityStore(authentication)
        self.origin = provider_origin
        self.context_factory, self.authority_context_factory = context_factory, authority_context_factory
        self.clock = clock
        self._lock = authentication.database.dependency_authority_gate
        self._actions = RLock()

    def _binding(self, policy):
        if type(policy) is not RuntimePolicy:
            raise RuntimeAuthorizationDenied('reissue_identity_unavailable')
        require_role(policy, 'creator')
        if policy.identity.session_id is not None:
            original = TextAnalysisIdentityOwner(self.authentication).capture(policy)
            return {name: getattr(original, name) for name in (
                'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
        authority = policy.identity_authority
        if (authority is None or not set(ACCOUNT_AUTHORITY_GRANT_TYPES).issubset(authority.grant_types)
                or not self.authentication.runtime_policy_is_current(policy)):
            raise RuntimeAuthorizationDenied('reissue_identity_unavailable')
        return {name: getattr(authority, name) for name in (
            'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}

    def _owned(self, policy, record, *, reopen=False):
        with self._lock:
            _need(self._binding(policy) == record.metadata['binding']
                  and self.origin == record.metadata['provider_origin'], 'reissue_binding_changed')
            self.store.assert_current(record)
            key = (self.client.key_authority.reopen_existing() if reopen
                   else self.authentication.installation_key_reference())
            _need(key is not None and key.provider_name == record.metadata['provider_name']
                  and key.provider_key_name == record.metadata['provider_key_name']
                  and key.installation_key_id == record.metadata['binding']['installation_key_id']
                  and key.installation_key_jkt == record.metadata['binding']['installation_key_jkt']
                  and canonical(json.loads(key.public_key_jwk)) == canonical(
                      json.loads(record.metadata['public_key_jwk'])), 'reissue_key_changed')

    def _context(self, record):
        context = ReissueContext(record.bodies['prepare_request'],
            record.metadata['public_key_jwk'].encode('utf-8'), record.metadata['operation_expires_at'])
        validate_context(context, schemas=self.client.schemas)
        _need(context.binding == record.metadata['binding']
              and context.operation_id == record.metadata['operation_id']
              and build_read_request(context, schemas=self.client.schemas) == record.bodies['read_request']
              and build_recover_request(context, schemas=self.client.schemas) == record.bodies['recover_request'])
        return context

    def _projection(self, record, context):
        raw = record.bodies['prepare_response']
        previous = None if raw is None else validate_reissue_projection(raw, operation='prepare',
            context=context, schemas=self.client.schemas, now=self.clock())
        if record.bodies['confirmation'] is not None:
            confirmation = validate_confirmation(record.bodies['confirmation'], context=context, schemas=self.client.schemas)
            _need(confirmation.expires_at == record.metadata['confirmation_expires_at'])
            previous = validate_reissue_projection(record.bodies['authorized_response'], operation='read',
                context=context, schemas=self.client.schemas, now=self.clock(), previous=previous)
            _need(previous.confirmation == confirmation)
        return previous

    def _status(self, record):
        context = self._context(record)
        projection = self._projection(record, context)
        if record.state == 'adopted':
            confirmation = projection.confirmation
            validate_reissue_result(record.bodies['result'], confirmation=confirmation,
                original_finalization_request=record.bodies['finalize_request'],
                authority_context=self.authority_context_factory(confirmation),
                trust=self.client.trust, schemas=self.client.schemas, previous_response=record.bodies['result'])
        navigation = (record.state == 'awaiting_authorization' and projection is not None
                      and projection.entry_reference is not None and self.clock() < _deadline(context.operation_expires_at))
        return ReissueStatus(record.flow_id, record.state, navigation)

    def status(self, policy, flow_id=None):
        with self._lock:
            binding = self._binding(policy)
            record = (self.store.latest(scope_digest(self.origin, binding)) if flow_id is None
                      else self.store.get(flow_id))
            if record is None:
                return None
            self._owned(policy, record)
            return self._status(record)

    def navigation(self, policy, flow_id):
        with self._lock:
            record = self.store.get(flow_id)
            self._owned(policy, record)
            if not self._status(record).navigation_available:
                return None
            projection = self._projection(record, self._context(record))
            return browser_entry_url(self.origin, projection.entry_reference)

    def begin(self, policy):
        with self._actions:
            with self._lock:
                binding = self._binding(policy)
                existing = self.store.active(scope_digest(self.origin, binding))
                if existing is not None:
                    self._owned(policy, existing)
                    return self._status(existing)
                persisted = self.authentication.installation_key_reference()
                _need(persisted is not None and (persisted.installation_key_id, persisted.installation_key_jkt) ==
                      (binding['installation_key_id'], binding['installation_key_jkt']), 'reissue_key_changed')
                key = self.client.key_authority.ensure_ready()
                _need(key == persisted, 'reissue_key_changed')
                context = validate_context(self.context_factory(policy, key), schemas=self.client.schemas)
                _need(context.binding == binding and context.operation_expires_at is None
                      and canonical(json.loads(context.public_key_jwk)) ==
                      canonical(json.loads(key.public_key_jwk)))
                record = self.store.begin(origin=self.origin, context=context, provider_name=key.provider_name,
                    provider_key_name=key.provider_key_name, read_request=build_read_request(context, schemas=self.client.schemas),
                    recover_request=build_recover_request(context, schemas=self.client.schemas))
                _need(record.state == 'prepared' and record.bodies['prepare_response'] is None, 'reissue_conflict')
            return self._advance(policy, record)

    def resume(self, policy, flow_id):
        with self._actions:
            record = self.store.get(flow_id)
            self._owned(policy, record, reopen=True)
            return self._advance(policy, record)

    def _advance(self, policy, record):
        self._owned(policy, record, reopen=True)
        context = self._context(record)
        if record.state == 'adopted':
            return self._status(record)
        if record.bodies['prepare_response'] is None:
            prepared = self.client.prepare(context, before_send=lambda: self._owned(policy, record, reopen=True))
            with self._lock:
                self._owned(policy, record)
                record = self.store.prepared(record, prepared)
            context = self._context(record)
            # Hosted consent is independent; begin returns the first navigation.
            return self._status(record)
        previous = self._projection(record, context)
        confirmation = previous.confirmation
        authority_context = None if confirmation is None else self.authority_context_factory(confirmation)
        if record.bodies['finalize_request'] is not None:
            try:
                recovered = self.client.recover(context, confirmation=confirmation,
                    authority_context=authority_context,
                    original_finalization_request=record.bodies['finalize_request'],
                    previous_response=record.bodies['result'],
                    before_send=lambda: self._owned(policy, record, reopen=True))
            except TextAnalysisReissueError as error:
                if error.code != 'not_completed':
                    raise
            else:
                return self._adopt(policy, record, recovered, authority_context)
        observed = self.client.read(context, confirmation=confirmation, authority_context=authority_context,
            original_finalization_request=record.bodies['finalize_request'], previous_response=record.bodies['result'],
            previous=previous, before_send=lambda: self._owned(policy, record, reopen=True))
        if type(observed) is ValidatedReissueResult:
            return self._adopt(policy, record, observed, authority_context)
        if observed.state == 'awaiting-authorization':
            if self.clock() >= _deadline(context.operation_expires_at):
                with self._lock:
                    self._owned(policy, record)
                    record = self.store.expired(record)
            return self._status(record)
        if observed.state == 'expired':
            with self._lock:
                self._owned(policy, record)
                record = self.store.expired(record)
            return self._status(record)
        _need(observed.state == 'authorized' and observed.confirmation is not None)
        with self._lock:
            self._owned(policy, record)
            record = self.store.authorized(record, observed)
        confirmation = observed.confirmation
        authority_context = self.authority_context_factory(confirmation)
        # Completed reads above precede all creation-deadline checks.
        if self.clock() >= min(_deadline(context.operation_expires_at), _deadline(confirmation.expires_at)):
            with self._lock:
                self._owned(policy, record)
                record = self.store.expired(record)
            return self._status(record)
        inner = build_finalize_request(confirmation, schemas=self.client.schemas, now=self.clock())
        with self._lock:
            self._owned(policy, record)
            record = self.store.finalization(record, inner)
        result = self.client.finalize(confirmation, authority_context=authority_context,
            original_finalization_request=inner, before_send=lambda: self._owned(policy, record, reopen=True))
        return self._adopt(policy, record, result, authority_context)

    def _adopt(self, policy, record, result, authority_context):
        with self._lock:
            self._owned(policy, record)
            record = self.store.adopt(record, result, authority_store=self.authorities,
                authority_context=authority_context, trust=self.client.trust, schemas=self.client.schemas, policy=policy)
            return self._status(record)


def _deadline(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)


_service = None


def configure_reissue_service(service):
    global _service
    if service is not None and type(service) is not TextAnalysisReissueService:
        raise ValueError('reissue_service_invalid')
    _service = service


def reissue_service():
    return _service
