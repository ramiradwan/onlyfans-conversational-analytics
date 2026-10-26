"""Explicit current-view reconstruction from immutable canonical findings."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
import hashlib
import json
import time
from uuid import UUID

from app.analytics.nlp_contribution_replay import replay_contribution
from app.analytics.nlp_findings_store import FindingsStore, encoded, checked_snapshot
from app.analytics.nlp_processing import FindingsReadContext, FrozenFindingsPipeline
from app.analytics.nlp_read_projection import frozen_labels
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.nlp.profiles import read_profile
from app.persistence.contributions import ContributionRepository
from app.persistence.nlp_dependencies import DependencyPersistence
from app.security.runtime_policy import RuntimePolicy, authorized_account


@dataclass(frozen=True)
class RecoveryTicket:
    account: str
    intent: str
    profile: bytes
    references: tuple[tuple[str, str], ...]
    authority: tuple
    selection: str
    source_token: str
    presentation: bytes | None
    normalization: bytes | None
    phase: str = 'inventory'
    reference_index: int = 0
    after_ordinal: int = -1


def _resolver(profile, raw):
    if raw is None:
        return None
    if type(raw) is not bytes or len(raw) > 65536:
        raise ValueError('nlp_recovery_presentation_invalid')
    labels = json.loads(raw)
    def resolve(identity):
        matches = [item for item in labels if all(item.get(k) == v for k, v in identity.items())]
        if len(matches) != 1:
            raise ValueError('nlp_recovery_presentation_invalid')
        if matches[0].get('label_status') == 'id_only':
            return None
        return {**identity, 'display_label': matches[0]['display_label']}
    if frozen_labels(profile, resolve) != labels:
        raise ValueError('nlp_recovery_presentation_invalid')
    return resolve


class RecoveryCoordinator:
    """Own no worker; callers explicitly advance bounded recovery pages."""
    def __init__(self, repository, authority, source, projections, *, policy, clock,
                 cancellation_check=None, deadline=None):
        if type(repository) is not ContributionRepository or type(authority) is not DependencyPersistence:
            raise ValueError('nlp_recovery_owner_required')
        if type(policy) is not RuntimePolicy or source.history.database is not repository.database:
            raise ValueError('nlp_recovery_owner_mismatch')
        self.repository, self.authority, self.source = repository, authority, source
        self.projections, self.policy, self.clock = projections, policy, clock
        self.cancelled, self.deadline = cancellation_check, deadline
        self.findings = FindingsStore(projections.database)
        self.reader = FindingsReadContext(source, projections, authorize=self.authorize,
            clock=clock, cancellation_check=self._stop_requested)
        projections.database.bind_contribution_origins(repository.origin_is_live, repository.origin_epoch)

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise ValueError('nlp_recovery_cancelled')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError('nlp_recovery_deadline')

    def _stop_requested(self):
        return ((self.cancelled is not None and self.cancelled())
                or (self.deadline is not None and time.monotonic() >= self.deadline))

    def authorize(self, account):
        self.check()
        authorized_account(self.policy, account)
        if self.authority.read_epoch() != self.policy.authorization_epoch.value:
            raise ValueError('nlp_recovery_read_authority_changed')
        if self.policy.expires_at is not None and self.policy.expires_at <= self.clock():
            raise ValueError('nlp_recovery_read_expired')

    def _records(self, account, profile, references):
        """Preflight the whole pinned selection before permitting publication."""
        if (type(references) is not tuple or not 1 <= len(references) <= 64
                or any(type(r) is not tuple or len(r) != 2 or any(type(v) is not str for v in r) for r in references)
                or len({r[0] for r in references}) != len(references)):
            raise ValueError('nlp_recovery_references_invalid')
        records = {}
        for contribution, expected in references:
            self.authorize(account)
            value = self.repository.export_document(contribution, policy=self.policy)
            manifest = value['manifest']
            if (hashlib.sha256(encoded(manifest)).hexdigest() != expected
                    or manifest['profile_sha256'] != profile.identity_digest()):
                raise ValueError('nlp_recovery_manifest_changed')
            for item in value['members']:
                self.check()
                raw = item['record_utf8'].encode('utf8')
                record = json.loads(raw)
                if record['source']['account_scope'] != account_ref(account):
                    raise ValueError('nlp_recovery_account_mismatch')
                key = record['source']['source_ref']
                if key in records and records[key] != raw:
                    raise ValueError('nlp_recovery_result_conflict')
                records[key] = raw
                if len(records) > 100000:
                    raise ValueError('nlp_recovery_record_limit')
        return records

    def _selection(self, account):
        return encoded(self.findings.selection_token(account)).decode()

    def _current_owner(self, account, references):
        for contribution, _ in references:
            try:
                with self.repository.read_committed(contribution, policy=self.policy) as (db, manifest, spec, _):
                    self.authorize(account)
                    if spec.creator_account_id != account:
                        raise ValueError('nlp_recovery_account_mismatch')
                    current = db.execute('SELECT enrollment_id FROM text_analysis_account_current '
                        'WHERE installation_id=? AND creator_account_id=?', (spec.installation_id, account)).fetchone()
                    if current is None or current['enrollment_id'] is None:
                        continue
                    intent = db.execute('SELECT enrollment_id FROM text_analysis_intents WHERE job_id=? '
                        'AND contribution_id=? AND installation_id=? AND creator_account_id=?',
                        (manifest.job_id, contribution, spec.installation_id, account)).fetchone()
                    if intent is None or intent['enrollment_id'] != current['enrollment_id']:
                        raise ValueError('nlp_recovery_current_owner_conflict')
            except ValueError:
                self.authorize(account)
                raise

    @contextmanager
    def visibility(self, ticket):
        self.authorize(ticket.account)
        profile = read_profile(ticket.profile)
        with self.authority.recovery_visibility(profile.identity_digest(), expected=ticket.authority):
            self.authorize(ticket.account)
            self._current_owner(ticket.account, ticket.references)
            self._check_selection(ticket)
            if self.source.erasure_source_identity(ticket.account) != ticket.source_token:
                raise ValueError('nlp_recovery_source_changed')
            # Generation activation owns canonical writer transactions itself.
            yield
            self._current_owner(ticket.account, ticket.references)
            self._check_selection(ticket)
            self.authorize(ticket.account)

    @contextmanager
    def literal_visibility(self, ticket):
        with self.visibility(ticket):
            with self.source.dependency_selection_guard(ticket.account, ticket.source_token):
                self.authorize(ticket.account)
                yield

    def _check_selection(self, ticket):
        self.check()
        if type(ticket) is not RecoveryTicket or ticket.phase not in ('inventory', 'records', 'literals', 'complete'):
            raise ValueError('nlp_recovery_ticket_invalid')
        from app.analytics.nlp_literals import _POLICY_BYTES
        if ticket.normalization is not None and ticket.normalization != _POLICY_BYTES:
            raise ValueError('nlp_recovery_normalization_invalid')
        _resolver(read_profile(ticket.profile), ticket.presentation)
        state = self.findings.state(ticket.account)
        if (self._selection(ticket.account) != ticket.selection or state['mode'] != 'paused'
                or read_profile(state['profile_json']).identity_digest() != read_profile(ticket.profile).identity_digest()):
            raise ValueError('nlp_recovery_selection_changed')

    def begin(self, account, profile, references, *, intent, presentation=None, normalization=None):
        self.authorize(account)
        if type(intent) is not str or str(UUID(intent)) != intent or UUID(intent).version != 4:
            raise ValueError('nlp_recovery_intent_invalid')
        profile = read_profile(profile.model_dump_json())
        _resolver(profile, presentation)
        from app.analytics.nlp_literals import _POLICY_BYTES
        if normalization is not None and type(normalization) is not bytes:
            raise ValueError('nlp_recovery_normalization_invalid')
        # Unsupported original policy makes only the supplementary view unavailable.
        normalization = normalization if normalization == _POLICY_BYTES else None
        self._records(account, profile, references)
        token = self.source.erasure_source_identity(account)
        with self.authority.recovery_visibility(profile.identity_digest()) as barrier:
            self.authorize(account)
            self._current_owner(account, references)
            with self.source.dependency_selection_guard(account, token):
                current = self.findings.selection_token(account)
                try:
                    state = self.findings.state(account)
                except ValueError as error:
                    if str(error) != 'nlp_account_missing':
                        raise
                    state = None
                if state is not None and state['mode'] == 'enabled':
                    raise ValueError('nlp_recovery_selection_busy')
                if state is not None and state['mode'] == 'withdrawn':
                    raise ValueError('nlp_profile_withdrawn')
                if state is not None and state['mode'] == 'removed' and state['dependency_intent'] == intent:
                    raise ValueError('nlp_recovery_intent_stale')
                if state is None or state['mode'] == 'removed':
                    self.authorize(account)
                    self.findings.select(account, profile, mode='paused', expected=current, intent=intent)
                elif read_profile(state['profile_json']).identity_digest() != profile.identity_digest():
                    raise ValueError('nlp_recovery_profile_conflict')
                ticket = RecoveryTicket(account, intent, profile.model_dump_json().encode(), references,
                    barrier, self._selection(account), token, presentation, normalization)
        return ticket

    def step(self, ticket, *, limit=64):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('nlp_recovery_page_invalid')
        with self.visibility(ticket):
            pass
        if ticket.phase == 'inventory':
            complete = self.reader.reconcile(ticket.account, maximum=limit)
            return replace(ticket, phase='records' if complete else 'inventory')
        if ticket.phase == 'records':
            if ticket.reference_index >= len(ticket.references):
                return replace(ticket, phase='literals')
            contribution, _ = ticket.references[ticket.reference_index]
            with self.visibility(ticket):
                result = replay_contribution(self.repository, self.findings, contribution, policy=self.policy,
                    after_ordinal=ticket.after_ordinal, limit=limit)
            return replace(ticket, reference_index=ticket.reference_index + int(result['complete']),
                after_ordinal=-1 if result['complete'] else result['after_ordinal'])
        if ticket.phase == 'literals':
            if ticket.normalization is None:
                return replace(ticket, phase='complete')
            frozen = self.findings.freeze(ticket.account, self.source.read_identity(ticket.account),
                label_resolver=_resolver(read_profile(ticket.profile), ticket.presentation))
            count = self.reader.backfill_literals(ticket.account, frozen, maximum=limit)
            return replace(ticket, phase='complete' if count < limit else 'literals')
        if ticket.phase != 'complete':
            raise ValueError('nlp_recovery_phase_invalid')
        return ticket

    def finish(self, ticket):
        if ticket.phase != 'complete':
            raise ValueError('nlp_recovery_incomplete')
        with self.visibility(ticket):
            pass
        profile = read_profile(ticket.profile)
        expected = {hashlib.sha256(raw).hexdigest() for raw in self._records(ticket.account, profile, ticket.references).values()}
        canonical, _ = self.reader._admit_inventory(ticket.account)
        state = self.findings.state(ticket.account)
        if not state['scan_complete']:
            raise ValueError('nlp_recovery_incomplete')
        frozen = self.findings.freeze(ticket.account, canonical, label_resolver=_resolver(profile, ticket.presentation))
        with self.projections.database.read() as db:
            value = checked_snapshot(db, frozen)
        if set(value['records']) != expected:
            raise ValueError('nlp_recovery_record_conflict')
        pipeline = FrozenFindingsPipeline(self.source, projections=self.projections, clock=self.clock,
            nlp_findings=frozen, nlp_authorize=partial(self.authorize, ticket.account),
            nlp_admit=partial(self.reader._publication_admission, ticket.account),
            on_published=partial(self.findings.acknowledge, ticket.account, state['notification_revision']),
            dependency_guard=partial(self.visibility, ticket))
        candidate = pipeline.build_candidate(ticket.account, cancellation_check=self._stop_requested)
        result = pipeline.publish_candidate(candidate)
        literal = None
        if ticket.normalization is not None:
            from app.analytics.nlp_literals import publish
            budget = QuestionBudget(QuestionLimits(max_records=100000, wall_clock_ms=30000), cancellation_check=self._stop_requested)
            with self.source.open_question_scope(ticket.account, budget, verify_source_token=True, prepare_identity=True) as scope:
                literal = publish(self.projections.database, frozen, canonical=scope.identity,
                    source_current=partial(scope.check, budget), check=partial(self.authorize, ticket.account),
                    visibility_guard=partial(self.literal_visibility, ticket))
        return {'snapshot': frozen, 'generation': result, 'literal_publication': literal,
                'literal_availability': 'current' if literal else 'unavailable'}
