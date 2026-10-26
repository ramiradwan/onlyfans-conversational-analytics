"""Explicit synthetic inputs for the real browser/application boundary."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time

SCENARIOS = ('joint', 'split-partial', 'empty', 'saved-only')
MARKER = '.e2e-text-analysis.json'
SOURCE = Path(__file__).resolve().parents[1] / 'fixtures/text-analysis.json'
PRODUCT_ROOT = Path(__file__).resolve().parents[3]
MAX_JSON = 65_536


def _require(condition):
    if not condition:
        raise ValueError('text_analysis_fixture_invalid')


def _json(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_JSON)
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value)
            value[key] = item
        return value
    def constant(_):
        raise ValueError('text_analysis_fixture_invalid')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def source_fixture():
    value = _json(SOURCE.read_bytes())
    _require(value['schema'] == 'e2e-text-analysis-source.v1' and len(value['messages']) == 2)
    for message in value['messages']:
        _require(message['direction'] in ('inbound', 'outbound') and len(message['mentions']) == 2)
        for span in message['mentions']:
            _require(message['text'][span['start']:span['end']] == span['text'] == 'café')
        if 'secondary_mention' in message:
            span = message['secondary_mention']
            _require(message['text'][span['start']:span['end']] == span['text'] == 'tea')
    return value


@dataclass(frozen=True)
class FixturePaths:
    root: Path
    auth: Path
    canonical: Path
    projection: Path
    analytics: Path


def validate_paths(settings, scenario):
    from app.core.optional_execution import require_optional_execution
    from app.persistence.private_files import reject_path_aliases
    require_optional_execution()
    _require(scenario in SCENARIOS and settings.websocket_auth_mode == 'local_session'
             and settings.identity_binding_source == 'verified_grants')
    names = ('auth.sqlite3', 'canonical.sqlite3', 'projections.sqlite3', 'analytics-projections.sqlite3')
    candidates = (settings.auth_database_path, settings.canonical_database_path,
                  settings.projection_database_path, settings.analytics_projection_database_path)
    paths = tuple(reject_path_aliases(path) for path in candidates)
    _require(all(Path(path).is_absolute() for path in candidates))
    root = paths[0].parent
    _require(root.is_dir() and root.name.startswith('ofca-text-analysis-')
             and tuple(path.name for path in paths) == names
             and all(path.parent == root for path in paths) and len(set(paths)) == 4)
    _require(all(not path.exists() or path.is_file() and path.stat().st_nlink == 1 for path in paths))
    return FixturePaths(root, *paths)


def _publish(path, value):
    from app.persistence.private_files import (
        apply_private_file_security, private_file_identity, sync_directory,
    )
    raw = _bytes(value)
    _require(len(raw) <= MAX_JSON)
    temporary = path.with_name(path.name + '.writing')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        apply_private_file_security(temporary)
        identity = private_file_identity(temporary, repair_permissions=False)
        with os.fdopen(descriptor, 'wb', closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _require(temporary.read_bytes() == raw
                 and private_file_identity(temporary, repair_permissions=False) == identity)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        sync_directory(path.parent)
        _require(private_file_identity(path, repair_permissions=False) == identity)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def prepare_paths(settings, scenario):
    """Validate ownership before application imports can create databases."""
    from app.persistence.private_files import apply_private_directory_security, private_file_identity
    paths = validate_paths(settings, scenario)
    marker = paths.root / MARKER
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if marker.exists():
        _require(marker.stat().st_nlink == 1 and not marker.with_name(marker.name + '.writing').exists())
        apply_private_directory_security(paths.root, repair_permissions=False)
        private_file_identity(marker, repair_permissions=False)
        document = _json(marker.read_bytes())
        _require(_bytes(document) == marker.read_bytes())
        _require(set(document) == {'schema', 'phase', 'scenario', 'fixture_sha256', 'scope',
            'principal_id', 'account_id', 'grant_reference_ids', 'auth_store_id', 'canonical_store_id',
            'profile_sha256', 'contexts'})
        _require(document.get('schema') == 'e2e-text-analysis-store.v1'
                 and document.get('phase') == 'ready' and document.get('fixture_sha256') == digest
                 and document.get('scenario') == ('joint' if scenario == 'saved-only' else scenario)
                 and all(path.is_file() for path in (paths.auth, paths.canonical, paths.projection, paths.analytics)))
        return paths, document
    _require(scenario != 'saved-only' and not tuple(paths.root.iterdir()))
    apply_private_directory_security(paths.root)
    _publish(marker, {'schema': 'e2e-text-analysis-store.v1', 'phase': 'initializing',
                     'scenario': scenario, 'fixture_sha256': digest})
    return paths, None


def worker_response(profile, scenario, request, cancelled):
    """Return source-bound typed output from fixed synthetic source text."""
    _require(scenario in ('joint', 'split-partial', 'empty') and not cancelled())
    messages = {row['text']: row for row in source_fixture()['messages']}
    component = profile.component(request.component_id)
    items = []
    for item in request.items:
        _require(item.text in messages and hashlib.sha256(item.text.encode()).hexdigest() == item.text_sha256)
        tasks = []
        for task in component.tasks:
            partial = scenario == 'split-partial' and task.task == 'message_topics'
            coverage = {'source_codepoints': len(item.text),
                        'examined': [{'start': 0, 'end': 6 if partial else len(item.text)}],
                        'excluded': [{'start': 6, 'end': len(item.text)}] if partial else [],
                        'limits_reached': ['windows'] if partial else []}
            findings = [] if scenario == 'empty' else (
                [{'topic_id': task.enabled_ids[0]}] if task.task == 'message_topics' else
                [{'concept_id': task.enabled_ids[0], 'span': {'start': span['start'], 'end': span['end']}}
                 for span in messages[item.text]['mentions']])
            secondary = messages[item.text].get('secondary_mention')
            if scenario != 'empty' and secondary is not None:
                findings.append({'topic_id': task.enabled_ids[1]} if task.task == 'message_topics' else
                    {'concept_id': task.enabled_ids[1], 'span': {'start': secondary['start'], 'end': secondary['end']}})
            tasks.append({'component_id': component.component_id, 'component_sha256': component.identity_digest(),
                'text_sha256': item.text_sha256, 'payload': {'task': task.task,
                'outcome': 'partial' if partial else ('assessed_findings' if findings else 'assessed_no_match'),
                'reason': 'limit_reached' if partial else 'none', 'coverage': coverage, 'findings': findings}})
        items.append({'request_item_id': item.request_item_id, 'tasks': tasks})
    return _bytes({'protocol': 'nlp-worker.v2', 'request_id': request.request_id,
        'profile_sha256': request.profile_sha256, 'component_id': request.component_id,
        'component_sha256': request.component_sha256, 'items': items})


class NoNetwork:
    def request(self, *args, **kwargs):
        raise RuntimeError('text_analysis_fixture_network_unavailable')

    def close(self):
        pass


class FixtureOwner:
    def __init__(self, main, paths, scenario, marker):
        from app.bootstrap import history_source, transport_manager
        from app.persistence.auth import SQLiteAuthenticationStore
        from app.persistence.contributions import ContributionRepository
        from app.analytics.nlp_contribution_sources import ContributionSourceGuard
        from app.persistence.authoritative_pair import AuthoritativePairOwner, FixtureAuthoritativeRootLease
        from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
        from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
        from app.persistence.text_analysis_jobs import TextAnalysisJobStore
        from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
        from app.security.text_analysis_authority import fixture_trust
        from app.security.text_analysis_contract import TextAnalysisSchemas
        from app.security.text_analysis_identity import TextAnalysisIdentityOwner
        _require(main._findings_configuration is None and main._grant_refresh is None)
        self.main, self.paths, self.scenario = main, paths, scenario
        self.source, self.canonical = history_source, transport_manager.canonical_database
        _require(self.source.history.database is self.canonical and self.canonical.path == paths.canonical)
        self.auth = SQLiteAuthenticationStore(paths.auth)
        self.identity_owner = TextAnalysisIdentityOwner(self.auth)
        self.repository = ContributionRepository(self.canonical, source_guard=ContributionSourceGuard())
        self.authority = TextAnalysisAuthorityStore(self.auth)
        self.trust, self.schemas = fixture_trust(environment='test'), TextAnalysisSchemas()
        cases = json.loads((PRODUCT_ROOT/'contracts/text-analysis-authority-v1/signed-cases.json').read_bytes())
        self.cases = {kind: next(row for row in cases if row['case_id'] == kind + '-accepted')
                      for kind in ('license', 'preview')}
        self.scope = {name: self.cases['license']['payload'][name] for name in
                      ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
        self.account = source_fixture()['account_id']
        self.pair = AuthoritativePairOwner(self.auth, self.canonical,
            installation_id=self.scope['installation_id'], root_lease=FixtureAuthoritativeRootLease(paths.root))
        self.enrollments = TextAnalysisEnrollmentStore(TextAnalysisJobStore(self.repository))
        self.preview_store = TextAnalysisPreviewStore(self.repository)
        self.engine = self._engine()
        self.dependencies = self.client = None
        self._lock = threading.Lock()
        self.prepare_count = self.analyze_count = 0
        self.original = marker
        if marker is None:
            self._initialize()
        else:
            self._reopen(marker)
        self._configure()

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def _engine(self):
        from tests.test_nlp_dependencies import Engine
        owner = self
        class DeterministicEngine(Engine):
            @contextmanager
            def open(self, profile, available, check, *, deadline=None):
                _require(self.present)
                with super().open(profile, available, check, deadline=deadline) as session:
                    original = session.prepare
                    def prepare(*args, **kwargs):
                        with owner._lock:
                            owner.prepare_count += 1
                        return original(*args, **kwargs)
                    def analyze(request, cancelled, **bounds):
                        check()
                        with owner._lock:
                            owner.analyze_count += 1
                        return worker_response(profile, owner.scenario, request, cancelled)
                    session.prepare, session.analyze = prepare, analyze
                    yield session
        return DeterministicEngine()

    def _profile(self):
        from app.nlp.profiles import AnalysisProfile
        from tests.test_nlp_artifact_store import example
        profile, payloads = example('split' if self.scenario == 'split-partial' else 'joint')
        value = profile.model_dump(mode='json')
        for component in value['components']:
            for task in component['tasks']:
                label = 'topic.secondary' if task['task'] == 'message_topics' else 'concept.secondary'
                task['enabled_ids'].append(label)
                component['executed_labels'].append({'task': task['task'], 'label_id': label,
                                                     'description': 'Secondary synthetic category'})
        return AnalysisProfile.model_validate_json(_bytes(value)), payloads

    def _seed_identity(self):
        from app.persistence.auth import (InstallationKeyReservation, InstallationKeyReference,
            ProvisioningCandidate, ProvisioningCandidateState, AuthorizedAccountBinding)
        from app.security.hosted_grants import HostedGrantClient
        from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
        from app.security.runtime_policy import AuthContext
        from tests.test_hosted_grants import signed_bundle, _payload, _token
        now = self.now()
        bundle = signed_bundle(iat=int(now.timestamp()) - 60)
        proof = json.loads((PRODUCT_ROOT/'contracts/text-analysis-authority-v1/proof-cases.json').read_bytes())[0]
        public = proof['public_key']
        from app.security.signed_objects import encode
        _require(encode(hashlib.sha256(_bytes(public)).digest()) == self.scope['installation_key_jkt'])
        reservation = self.auth.reserve_installation_key(InstallationKeyReservation(
            'E2E Text Analysis Fixture', 'synthetic-existing-public-key', 'ECDSA_P256', now))
        key = InstallationKeyReference(reservation.provider_name, reservation.provider_key_name,
            reservation.algorithm, self.scope['installation_key_id'], self.scope['installation_key_jkt'],
            _bytes(dict(public, kid=self.scope['installation_key_id'])).decode(), reservation.created_at, now)
        self.auth.activate_installation_key(key)
        tokens = {}
        original = dict(bundle.tokens, creator_account_binding=next(iter(bundle.creator_bindings.values())))
        for kind in ACCOUNT_AUTHORITY_GRANT_TYPES:
            payload = dict(_payload(original[kind]), **self.scope)
            if kind == 'installation_grant':
                payload['sub'] = 'installation:' + self.scope['installation_id']
            elif kind == 'creator_account_binding':
                payload.update(creator_account_id=self.account,
                    sub=f"installation:{self.scope['installation_id']}:creator:{self.account}")
            else:
                payload['allowed_creator_account_ids'] = [self.account]
                payload['roles'] = ['owner']
                payload['scopes'] = ['commercial:manage', 'creator-binding:approve', 'creator-binding:revoke',
                    'installation-claim:issue', 'installation:provision', 'membership:manage',
                    'organization:manage', 'provisioning:milestone', 'runtime:existing-data',
                    'runtime:licensed-processing']
            purpose = 'membership' if kind == 'membership_snapshot' else 'installation-binding'
            trust = next(row for row in bundle.trust_set['keys'] if row['purpose'] == purpose)
            protected = {'installation_grant': 'installation', 'membership_snapshot': 'membership',
                         'creator_account_binding': 'creator-binding'}[kind]
            tokens[kind] = _token(payload, f'urn:bridge-clean:grant:{protected}:v1',
                                  trust['jwk'], bundle.purpose_keys[purpose])
        self.principal = _payload(tokens['membership_snapshot'])['ciam_subject']
        verifier = HostedGrantClient(NoNetwork(), None, self.auth, clock=self.now, trust_set=bundle.trust_set)
        grants = verifier._verify_bundle(tokens, grant_types=ACCOUNT_AUTHORITY_GRANT_TYPES,
            organization_id=self.scope['organization_id'], installation_id=self.scope['installation_id'],
            key=key, identity=AuthContext(self.principal, self.account, 'creator'))
        self.auth.record_verified_grants(grants)
        self.grant_ids = tuple(grant.reference_id for grant in grants)
        self.auth.record_provisioning_candidate(ProvisioningCandidate(
            association_request_id='e2e-text-analysis-association', installation_id=self.scope['installation_id'],
            onboarding_transaction_id='e2e-text-analysis-onboarding', organization_id=self.scope['organization_id'],
            creator_account_id=self.account, state=ProvisioningCandidateState.PENDING, requested_at=now))
        self.auth.approve_provisioning_candidate('e2e-text-analysis-association', resolved_at=now)
        self.auth.record_authorized_account_binding(AuthorizedAccountBinding(self.account,
            self.scope['installation_id'], self.account, 'e2e-text-analysis-association',
            hashlib.sha256('\n'.join(sorted(self.grant_ids)).encode()).hexdigest(), now, self.grant_ids))

    def _baseline(self, account=None):
        from app.models.text_analysis_enrollment import EnrollmentIdentity
        from app.security.runtime_policy import authorized_account
        original = EnrollmentIdentity(principal_id=self.principal, creator_account_id=self.account,
            role='creator', grant_reference_ids=self.grant_ids, **self.scope)
        policy = self.identity_owner.rebuild(original)
        authorized_account(policy, self.account if account is None else account)
        return policy

    def _seed_sources(self):
        now = self.now()
        with self.canonical.transaction() as db:
            _require(not db.execute('SELECT 1 FROM account_heads').fetchone())
            db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES(?,?,?)',
                       (self.account, 1, now.isoformat()))
            for ordinal, message in enumerate(source_fixture()['messages']):
                text = message['text']
                sent = now - timedelta(hours=2, minutes=ordinal)
                db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,
                    platform_user_id,display_name,content_hash,winning_stream_epoch,winning_source_seq,
                    is_deleted,updated_at) VALUES(?,?,'full','synthetic-fan',NULL,?,1,1,0,?)""",
                    (self.account, message['conversation_id'], hashlib.sha256(message['conversation_id'].encode()).hexdigest(), now.isoformat()))
                db.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,
                    sender_platform_user_id,text,sent_at,direction,content_hash,winning_stream_epoch,
                    winning_source_seq,is_deleted,updated_at) VALUES(?,?,?,'synthetic-fan',?,?,?,?,1,?,0,?)""",
                    (self.account, message['message_id'], message['conversation_id'], text, sent.isoformat(),
                     message['direction'], hashlib.sha256(text.encode()).hexdigest(), ordinal + 1, now.isoformat()))
            db.execute("""INSERT INTO projection_work(
                creator_account_id,canonical_revision,work_kind,conversation_id,created_at)
                VALUES(?,1,'reseed',NULL,?)""", (self.account, now.isoformat()))

    def _install_data(self):
        from app.nlp.artifact_store import ArtifactStore, StoreLimits
        from app.nlp.profile_authentication import authenticate_profile
        from app.nlp.profiles import canonical_bytes
        from app.nlp.release_catalog import canonical, verify_catalog, CATALOG_TYPE
        from app.persistence.nlp_dependencies import DependencyPersistence
        from tests.test_nlp_profile_authentication import keys, sign
        self.profile, payloads = self._profile()
        self.keys = keys()
        verified = authenticate_profile(sign(canonical_bytes(self.profile)), trusted_nlp_keys=self.keys,
                                       expected_profile_sha256=self.profile.identity_digest())
        self.artifacts = ArtifactStore(self.paths.root/'text-analysis-artifacts', StoreLimits(1024*1024, 4*1024*1024, 0))
        available = self.artifacts.stage(verified.profile, [(key, BytesIO(raw)) for key, raw in payloads.items()])
        persistence = DependencyPersistence(self.auth)
        persistence.initialize(hashlib.sha256(canonical(self.keys)).hexdigest())
        now = int(self.now().timestamp())
        document = {'schema_version': 'nlp-release-catalog.v1', 'sequence': 1, 'issued_at': now - 60,
            'expires_at': now + 3600, 'withdrawals': [], 'profiles': [{
                'profile_id': self.profile.profile_id, 'revision': self.profile.revision,
                'profile_sha256': self.profile.identity_digest(), 'compatibility': {
                    'profile_sha256': self.profile.identity_digest(),
                    'compatible_engines': [self.profile.components[0].engine.model_dump(mode='json')],
                    'adapters': [component.adapter.model_dump(mode='json') for component in self.profile.components]}}]}
        raw = sign(canonical(document), typ=CATALOG_TYPE)
        catalog, digest = verify_catalog(raw, self.keys)
        persistence.update(lambda state: {**state, 'sequence': catalog.sequence, 'catalog_sha256': digest,
            'catalog_jws': raw.decode(), 'clock_high_water': now, 'epoch': state['epoch'] + 1})
        persistence.install(verified.profile, available.data_closure_sha256)

    def _contexts(self, references):
        from app.analytics.nlp_preview import SELECTION_POLICY
        from app.nlp.execution_parameters import execution_parameters
        from app.nlp.preview_parameters import preview_parameters
        from app.nlp.profiles import artifact_closure_digest
        from app.security.text_analysis_policy import TextAnalysisRunContext
        closure = artifact_closure_digest(tuple(a for a in self.profile.artifacts
                                                if a.role in ('weights', 'tokenizer', 'configuration')))
        result = {}
        for kind, reference in references.items():
            parameters = execution_parameters(self.profile, closure) if kind == 'license' else preview_parameters(
                self.profile, closure, SELECTION_POLICY)
            payload = self.cases[kind]['payload']
            result[kind] = TextAnalysisRunContext(reference.reference_id, reference.compact_jws_sha256,
                self.principal, self.account, self.scope['organization_id'], self.scope['installation_id'],
                self.scope['installation_key_id'], self.scope['installation_key_jkt'], payload['seat_id'],
                payload['seat_scope'], 1, 1, 'text-analysis-artifact', self.profile.identity_digest(), closure,
                parameters.policy_identity_sha256)
        return result

    def _selection(self):
        from app.services.text_analysis_selection import FixtureTextAnalysisSelectionOwner, FixtureTextAnalysisSelection
        from app.services.text_analysis_preview_selection import FixtureTextAnalysisPreviewSelectionOwner, FixtureTextAnalysisPreviewSelection
        self.license_selection = FixtureTextAnalysisSelectionOwner(self.authority, self.enrollments, self.trust,
            self.schemas, self.scope['installation_id'],
            (FixtureTextAnalysisSelection(self.profile.identity_digest(), self.contexts['license']),), self.pair)
        self.preview_selection = FixtureTextAnalysisPreviewSelectionOwner(self.authority, self.preview_store,
            self.trust, self.schemas, self.scope['installation_id'],
            (FixtureTextAnalysisPreviewSelection(self.profile.identity_digest(), self.contexts['preview']),), self.pair)

    def _initialize(self):
        from app.security.text_analysis_policy import encode_run_context
        self._seed_identity()
        self._seed_sources()
        self._install_data()
        self.pair.establish()
        references = {kind: self.authority.import_verified_reference(case['token'], kind=kind,
            context=case['context'], trust=self.trust, schemas=self.schemas) for kind, case in self.cases.items()}
        self.contexts = self._contexts(references)
        self._selection()
        baseline = self._baseline()
        for kind, selection in (('license', self.license_selection), ('preview', self.preview_selection)):
            context = self.contexts[kind]
            if kind == 'preview':
                with self.authority.transition_fence(baseline, context, kind=kind, phase='import',
                        evidence=selection.evidence(baseline, context), lifecycle=selection.lifecycle, fixture_only=True) as held:
                    with self.canonical.transaction() as db:
                        self.preview_store.jobs.import_preview_in(db, encode_run_context(context), policy=held)
            self.authority.activate_reference(baseline, context, kind=kind,
                evidence=selection.evidence(baseline, context), lifecycle=selection.lifecycle, fixture_only=True)
            baseline = self._baseline()
        with self.auth.database.read() as db:
            auth_id = db.execute('SELECT store_id FROM auth_store_identity').fetchone()[0]
        with self.canonical.read() as db:
            canonical_id = db.execute('SELECT store_id FROM knowledge_store_identity').fetchone()[0]
        marker = {'schema': 'e2e-text-analysis-store.v1', 'phase': 'ready', 'scenario': self.scenario,
            'fixture_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(), 'scope': self.scope,
            'principal_id': self.principal, 'account_id': self.account, 'grant_reference_ids': list(self.grant_ids),
            'auth_store_id': auth_id, 'canonical_store_id': canonical_id,
            'profile_sha256': self.profile.identity_digest(),
            'contexts': {kind: encode_run_context(context).decode() for kind, context in self.contexts.items()}}
        _publish(self.paths.root/MARKER, marker)
        self.original = marker

    def _reopen(self, marker):
        from app.nlp.artifact_store import ArtifactStore, StoreLimits
        from app.security.text_analysis_policy import decode_run_context, encode_run_context
        from app.security.text_analysis_authority import verify_authority
        from app.security.text_analysis_references import reference_from_verified
        from tests.test_nlp_profile_authentication import keys
        self.profile, _ = self._profile()
        _require(marker['scope'] == self.scope and marker['account_id'] == self.account
                 and marker['profile_sha256'] == self.profile.identity_digest())
        self.principal, self.grant_ids = marker['principal_id'], tuple(marker['grant_reference_ids'])
        self.contexts = {kind: decode_run_context(raw.encode()) for kind, raw in marker['contexts'].items()}
        _require(set(self.contexts) == {'license', 'preview'})
        references = {}
        for kind, case in self.cases.items():
            decision = verify_authority(case['token'], kind=kind, context=case['context'],
                                        trust=self.trust, schemas=self.schemas)
            _require(decision.accepted)
            references[kind] = reference_from_verified(decision.authority)
        _require(all(encode_run_context(self._contexts(references)[kind]) == encode_run_context(self.contexts[kind])
                     for kind in references))
        self.keys = keys()
        self.artifacts = ArtifactStore(self.paths.root/'text-analysis-artifacts', StoreLimits(1024*1024, 4*1024*1024, 0))
        with self.auth.database.read() as db:
            _require(db.execute('SELECT store_id FROM auth_store_identity').fetchone()[0] == marker['auth_store_id'])
            for kind, reference in references.items():
                row = db.execute('SELECT kind,compact_jws FROM text_analysis_authority_references WHERE reference_id=?',
                                 (reference.reference_id,)).fetchone()
                _require(row is not None and tuple(row) == (kind, self.cases[kind]['token']))
        with self.canonical.read() as db:
            _require(db.execute('SELECT store_id FROM knowledge_store_identity').fetchone()[0] == marker['canonical_store_id'])
        with self.auth.database.dependency_authority_gate, self.auth.database.transaction() as auth_db:
            with self.canonical.read() as db:
                db.execute('BEGIN')
                self.pair.validate_in(auth_db, db)
        self._baseline()
        self._selection()
        if self.scenario == 'saved-only':
            for context in self.contexts.values():
                self.authority.revoke(context.reference_id)
            self.engine.present = False

    def _configure(self):
        from app.analytics.nlp_runtime import FindingsConfiguration
        from app.services.text_analysis_selection import FixtureTextAnalysisExecutionConfiguration
        from app.services.text_analysis_preview_selection import FixtureTextAnalysisPreviewConfiguration
        from app.security.grant_refresh import GrantRefreshLifecycle
        from app.security.hosted_grants import HostedGrantClient
        self.refresh = GrantRefreshLifecycle(lambda: self.auth,
            lambda auth: HostedGrantClient(NoNetwork(), None, auth, clock=self.now), clock=self.now,
            include_legacy_license_entitlement=False)
        _require(self.main._grant_refresh is None)
        self.main._grant_refresh = self.refresh
        self.main.configure_analytics_runtime(findings=FindingsConfiguration(
            dependencies=self.construct_dependencies, authorize=self._baseline, clock=self.now,
            execution=FixtureTextAnalysisExecutionConfiguration(self.auth, self.license_selection),
            preview=FixtureTextAnalysisPreviewConfiguration(self.auth, self.preview_selection)))
        self.main.app.router.add_event_handler('startup', self.after_startup)
        self._register_stats()

    def construct_dependencies(self, source, findings):
        from app.nlp.profiles import ResourcePolicy
        from app.persistence.nlp_dependencies import DependencyPersistence
        from app.services.nlp_dependencies import DependencyService
        from app.services.text_analysis_client import (
            FixtureTextAnalysisClientConfiguration, configure_client_service, client_service,
        )
        _require(source is self.source and source.history.database is self.canonical
                 and client_service() in (None, self.client))
        service = DependencyService(DependencyPersistence(self.auth), self.artifacts, findings, source, None,
            keys=self.keys, approvals=lambda profile: profile.identity_digest() == self.profile.identity_digest(),
            engine_provider=self.engine, resource_policy=ResourcePolicy(download_bytes=1024*1024,
                installed_bytes=4*1024*1024, temporary_bytes=4*1024*1024,
                transition_peak_memory_bytes=1024*1024, inference_threads=2),
            spool_root=self.paths.root/'text-analysis-spool', clock=lambda: self.now().timestamp())
        client = FixtureTextAnalysisClientConfiguration(self.auth, self.repository, self.pair).bind(source, service)
        self.dependencies, self.client = service, client
        configure_client_service(client)
        return service

    async def after_startup(self):
        from app.analytics import runtime
        from app.services.nlp_dependencies import dependency_service
        from app.services.text_analysis_execution import execution_service
        from app.services.text_analysis_preview import preview_service
        from app.services.text_analysis_client import client_service
        self.refresh.start()
        await runtime.launch_default_analytics_runtime()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            execution, preview = execution_service(), preview_service()
            if (self.dependencies is not None and dependency_service() is self.dependencies
                    and client_service() is self.client and execution is not None and preview is not None
                    and execution.dependencies is self.dependencies and preview.dependencies is self.dependencies):
                return
            await asyncio.sleep(.02)
        raise RuntimeError('text_analysis_fixture_startup_unavailable')

    def _register_stats(self):
        from fastapi import Depends, HTTPException
        from app.api.dependencies import get_authenticated_account_session, account_bound_to_session
        from app.api.security import require_creator
        def stats(policy=Depends(get_authenticated_account_session)):
            require_creator(policy)
            if account_bound_to_session(policy, None) != self.account:
                raise HTTPException(403, detail='Fixture account mismatch')
            try:
                self.identity_owner.capture(policy)
                result = self.snapshot()
                self.identity_owner.capture(policy)
            except ValueError:
                raise HTTPException(409, detail='Fixture observation unavailable') from None
            return result
        # The application SPA catch-all is already last; insert this literal route before it.
        self.main.app.add_api_route('/__e2e/text-analysis', stats, methods=['GET'])
        route = self.main.app.router.routes.pop()
        self.main.app.router.routes.insert(0, route)

    def snapshot(self):
        from app.analytics import runtime
        from app.services.nlp_dependencies import dependency_service
        from app.services.text_analysis_execution import execution_service
        from app.services.text_analysis_preview import preview_service
        from app.services.text_analysis_client import client_service
        execution, preview = execution_service(), preview_service()
        owned = runtime.analytics_runtime()
        with self.canonical.read() as db:
            db.execute('BEGIN')
            def count(table, where=''):
                value = db.execute('SELECT count(*) FROM ' + table + where).fetchone()[0]
                _require(type(value) is int and 0 <= value <= 1_000_000)
                return value
            canonical = {'committed_jobs': count('knowledge_jobs', " WHERE state='committed'"),
                'members': count('knowledge_members'), 'preview_outputs': count('text_analysis_preview_outputs'),
                'preview_spent': count('text_analysis_preview_admissions', " WHERE state='spent'"),
                'client_intents': count('text_analysis_client_intents')}
            source_count = db.execute('SELECT count(*) FROM account_messages WHERE creator_account_id=? AND is_deleted=0',
                                      (self.account,)).fetchone()[0]
            _require(type(source_count) is int and 0 <= source_count <= 1_000_000)
        with self._lock:
            # Engine counters cover this process, including no calls on saved-only restart.
            _require(0 <= self.prepare_count <= 1_000_000 and 0 <= self.analyze_count <= 1_000_000)
            engine = {'prepare_count': self.prepare_count, 'analyze_count': self.analyze_count}
        return {'schema': 'e2e-text-analysis.v1', 'scenario': self.scenario, 'account_id': self.account,
            'profile_id': self.profile.profile_id, 'profile_sha256': self.profile.identity_digest(),
            'source_count': source_count, 'engine': engine, 'canonical': canonical,
            'owners': {'canonical': owned.source.history.database is self.canonical,
                'source': owned.source is self.source,
                'authentication': self.dependencies.persistence.authentication is self.auth,
                'dependencies': dependency_service() is self.dependencies and owned.findings._service is self.dependencies,
                'execution': execution is not None and execution.dependencies is self.dependencies,
                'preview': preview is not None and preview.dependencies is self.dependencies,
                'client': client_service() is self.client and self.client.dependencies is self.dependencies}}

    async def close(self):
        from app.analytics import runtime
        from app.services.text_analysis_client import client_service, configure_client_service
        drained = await runtime.shutdown_default_analytics_runtime()
        if client_service() is self.client:
            configure_client_service(None)
        await self.refresh.stop()
        if self.main._grant_refresh is self.refresh:
            self.main._grant_refresh = None
        return drained


def install_fixture(main, paths, scenario, marker):
    from app.core.optional_execution import require_optional_execution
    require_optional_execution()
    owner = FixtureOwner.__new__(FixtureOwner)
    try:
        owner.__init__(main, paths, scenario, marker)
        return owner
    except BaseException:
        from app.services.text_analysis_client import client_service, configure_client_service
        if getattr(owner, 'client', None) is not None and client_service() is owner.client:
            configure_client_service(None)
        refresh = getattr(owner, 'refresh', None)
        if refresh is not None:
            refresh.close()
            if main._grant_refresh is refresh:
                main._grant_refresh = None
            main.configure_analytics_runtime(findings=None)
        raise


def read_stop(stream, *, command_seen=None):
    """The inherited control pipe accepts one exact bounded command."""
    if stream.readline(6) != b'stop\n':
        return False
    if command_seen is not None:
        try:
            command_seen()
        except Exception:
            pass
    return stream.read(1) == b''
