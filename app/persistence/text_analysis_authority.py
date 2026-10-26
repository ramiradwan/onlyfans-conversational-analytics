"""Auth-owned text-analysis references and bounded transition reservations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import re
from app.persistence import sqlite_api as sqlite3
from app.persistence.database import CanonicalSQLite

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.security.runtime_policy import RuntimeAuthorizationDenied, RuntimePolicy
from app.security.text_analysis_authority import TextAnalysisTrust, verify_authority
from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
from app.security.text_analysis_policy import TextAnalysisRunContext, encode_run_context, require_text_analysis, require_text_analysis_fence
from app.security.text_analysis_references import TextAnalysisReference, reference_from_verified


def trust_digest(trust: TextAnalysisTrust) -> str:
    return hashlib.sha256(canonical({kid: {'purpose': key.purpose, 'point':
        key.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint).hex()}
        for kid, key in trust.keys.items()})).hexdigest()


@dataclass(frozen=True, slots=True)
class TextAnalysisCurrentEvidence:
    """An immutable observation; fixture evidence is never production authority."""
    trust: TextAnalysisTrust
    trust_sha256: str
    authorization_epoch: int
    reference_version: int
    dependency_epoch: int
    dependency_trust_sha256: str
    dependency_catalog_sha256: str
    context_sha256: str
    activation_lineage_sha256: str | None
    seat_id: str
    installation_key_id: str
    installation_key_jkt: str
    fixture_only: bool

    def __post_init__(self):
        if type(self.trust) is not TextAnalysisTrust or type(self.fixture_only) is not bool:
            raise ValueError('invalid_text_analysis_evidence')
        if trust_digest(self.trust) != self.trust_sha256 or self.fixture_only != self.trust.fixture_only:
            raise ValueError('invalid_text_analysis_evidence')
        for name in ('authorization_epoch', 'dependency_epoch', 'reference_version'):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == 'reference_version' else 0):
                raise ValueError('invalid_text_analysis_evidence')
        for name in ('trust_sha256','dependency_trust_sha256','dependency_catalog_sha256','context_sha256','activation_lineage_sha256'):
            value = getattr(self, name)
            if value is None and name == 'activation_lineage_sha256':
                continue
            if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
                raise ValueError('invalid_text_analysis_evidence')


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisLifecycle:
    """Explicit test-only store ownership; not an interprocess lifecycle gate."""
    auth_database: object
    canonical_database: CanonicalSQLite | None = None
    fixture_only: bool = True
    pair_owner: object | None = None


_SEAL = object()


@dataclass(frozen=True, slots=True)
class TextAnalysisActivation:
    policy: RuntimePolicy
    evidence: TextAnalysisCurrentEvidence
    activated: bool


class TextAnalysisTransitionFence:
    """A short-lived reservation bound to one held auth connection."""
    __slots__ = ('_store', '_connection', '_context', '_evidence', '_context_sha256', '_epoch', '_active', '_lifecycle', '_phase', '_policy', 'fixture_only')

    def __init__(self, seal, store, connection, context, evidence, epoch, lifecycle, phase):
        if seal is not _SEAL:
            raise RuntimeAuthorizationDenied('text_analysis_transition_fence_missing')
        self._store = store
        self._connection = connection
        self._context = context
        self._evidence = evidence
        self._context_sha256 = evidence.context_sha256
        self._epoch = epoch
        self._lifecycle = lifecycle
        self._phase = phase
        self._policy = None
        self._active = True
        self.fixture_only = True

    def is_current(self, policy: RuntimePolicy, context_sha256: str, *, phase: str) -> bool:
        if not self._active or context_sha256 != self._context_sha256:
            return False
        try:
            self._store._check_current_in(self._connection, policy, self._context, self._evidence, phase=phase)
            return (self._connection.in_transaction
                and policy.authorization_epoch == self._epoch
                and self._store.authentication.runtime_policy_is_current_in(
                    self._connection, policy, now=self._store.authentication._clock()))
        except (sqlite3.Error, RuntimeAuthorizationDenied):
            return False

    def owns_canonical_database(self, database) -> bool:
        """Match the exact canonical owner captured by this live reservation."""
        if not self._active or type(database) is not CanonicalSQLite:
            return False
        try:
            return self._connection.in_transaction and self._lifecycle.canonical_database is database
        except sqlite3.Error:
            return False

    def validate_canonical_connection(self, database, connection):
        if not self.owns_canonical_database(database) or not database.owns_connection(connection) or not connection.in_transaction:
            raise RuntimeAuthorizationDenied('text_analysis_canonical_owner_mismatch')
        pair = self._lifecycle.pair_owner
        if pair is not None:
            from app.persistence.authoritative_pair import AuthoritativePairOwner
            if type(pair) is not AuthoritativePairOwner or pair.auth_database is not self._store.database or pair.canonical_database is not database:
                raise RuntimeAuthorizationDenied('authoritative_pair_unavailable')
            pair.validate_in(self._connection, connection)


class TextAnalysisAuthorityStore:
    def __init__(self, authentication):
        self.authentication = authentication
        self.database = authentication.database
        self._schemas = None

    def import_verified_reference(self, token: str, *, kind: str, context: dict,
                                  trust: TextAnalysisTrust, schemas: TextAnalysisSchemas) -> TextAnalysisReference:
        decision = verify_authority(token, kind=kind, context=context, trust=trust, schemas=schemas)
        if not decision.accepted:
            raise RuntimeAuthorizationDenied(decision.reason)
        reference = reference_from_verified(decision.authority)
        with self.database.transaction() as db:
            prior = db.execute('SELECT * FROM text_analysis_authority_references WHERE reference_id=?',
                               (reference.reference_id,)).fetchone()
            if prior is not None:
                self._compare_row(prior, reference)
                return reference
            collision = db.execute('SELECT 1 FROM text_analysis_authority_references WHERE kind=? AND (object_id=? OR issuance_id=?)',
                (kind, reference.object_id, reference.issuance_id)).fetchone()
            if collision is not None:
                raise RuntimeAuthorizationDenied('text_analysis_object_collision')
            db.execute('INSERT INTO text_analysis_authority_references '
                '(reference_id,kind,object_id,issuance_id,compact_jws,compact_jws_sha256,payload_sha256,signer_kid,fixture_only) '
                'VALUES(?,?,?,?,?,?,?,?,?)', (reference.reference_id, kind, reference.object_id,
                reference.issuance_id, token, reference.compact_jws_sha256, reference.payload_sha256,
                reference.signer_kid, int(reference.fixture_only)))
            self.authentication._increment_authorization_epoch(db)
        return reference

    @staticmethod
    def _compare_row(row, reference):
        expected = (reference.kind, reference.object_id, reference.issuance_id, reference.compact_jws,
            reference.compact_jws_sha256, reference.payload_sha256, reference.signer_kid, int(reference.fixture_only))
        actual = tuple(row[name] for name in ('kind','object_id','issuance_id','compact_jws',
            'compact_jws_sha256','payload_sha256','signer_kid','fixture_only'))
        if actual != expected:
            raise RuntimeAuthorizationDenied('text_analysis_reference_corrupt')

    def get_reference_in(self, connection, reference_id, *, context: dict, trust: TextAnalysisTrust,
                         schemas: TextAnalysisSchemas) -> TextAnalysisReference:
        row = connection.execute('SELECT * FROM text_analysis_authority_references WHERE reference_id=?', (reference_id,)).fetchone()
        if row is None or row['state'] == 'revoked':
            raise RuntimeAuthorizationDenied('text_analysis_reference_unavailable')
        decision = verify_authority(row['compact_jws'], kind=row['kind'], context=context, trust=trust, schemas=schemas)
        if not decision.accepted:
            raise RuntimeAuthorizationDenied(decision.reason)
        reference = reference_from_verified(decision.authority)
        self._compare_row(row, reference)
        if reference.reference_id != reference_id:
            raise RuntimeAuthorizationDenied('text_analysis_reference_corrupt')
        return reference

    def revoke(self, reference_id: str):
        with self.database.transaction() as db:
            changed = db.execute("UPDATE text_analysis_authority_references SET state='revoked',version=version+1 WHERE reference_id=? AND state<>'revoked'", (reference_id,)).rowcount
            if changed:
                self.authentication._increment_authorization_epoch(db)

    def compose_policy_in(self, connection, identity_policy: RuntimePolicy, reference_id: str, context: TextAnalysisRunContext,
                          *, trust: TextAnalysisTrust, schemas: TextAnalysisSchemas, kind: str, now: datetime) -> RuntimePolicy:
        if not self.authentication.runtime_policy_is_current_in(connection, identity_policy, now=now):
            raise RuntimeAuthorizationDenied('text_analysis_policy_stale')
        reference = self.get_reference_in(connection, reference_id, context=verification_context(context, kind), trust=trust, schemas=schemas)
        policy = replace(identity_policy, text_analysis_authority=reference, text_analysis_fence=None)
        require_text_analysis(policy, context, kind=kind, phase='prepare')
        return policy

    def activate_reference(self, identity_policy: RuntimePolicy, context: TextAnalysisRunContext, *, kind: str,
                           evidence: TextAnalysisCurrentEvidence | None = None,
                           lifecycle: FixtureTextAnalysisLifecycle | None = None, fixture_only: bool = False) -> TextAnalysisActivation:
        with self.transition_fence(identity_policy, context, kind=kind, phase='import', evidence=evidence,
                                   lifecycle=lifecycle, fixture_only=fixture_only) as held:
            result = self.activate_reference_in(held, context, kind=kind, lifecycle=lifecycle)
        return replace(result, policy=replace(result.policy, text_analysis_fence=None))

    def activate_reference_in(self, policy: RuntimePolicy, context: TextAnalysisRunContext, *, kind: str,
                              lifecycle: FixtureTextAnalysisLifecycle) -> TextAnalysisActivation:
        reference = require_text_analysis_fence(policy, context, kind=kind, phase='import')
        fence = policy.text_analysis_fence
        if fence._store is not self or fence._lifecycle is not lifecycle or fence._phase != 'import':
            raise RuntimeAuthorizationDenied('text_analysis_activation_owner_mismatch')
        db = fence._connection
        if kind == 'preview' or lifecycle.pair_owner is not None:
            canonical_database = lifecycle.canonical_database
            if type(canonical_database) is not CanonicalSQLite:
                raise RuntimeAuthorizationDenied('text_analysis_canonical_owner_missing')
            # A separate connection observes committed admission, never a caller's uncommitted row.
            with canonical_database.read() as canonical_db:
                canonical_db.execute('BEGIN')
                if lifecycle.pair_owner is not None:
                    lifecycle.pair_owner.validate_read_in(db,canonical_db)
                if kind == 'preview':
                    row = canonical_db.execute('SELECT installation_id,permit_id,reference_id,compact_jws_sha256,issuance_id '
                        'FROM text_analysis_preview_admissions WHERE installation_id=? AND permit_id=?',
                        (context.installation_id, reference.object_id)).fetchone()
                    expected = (context.installation_id, reference.object_id, reference.reference_id,
                                reference.compact_jws_sha256, reference.issuance_id)
                    if row is None or tuple(row) != expected:
                        raise RuntimeAuthorizationDenied('text_analysis_canonical_admission_mismatch')
        row = db.execute('SELECT state,version FROM text_analysis_authority_references WHERE reference_id=?',
                         (reference.reference_id,)).fetchone()
        if row is None or row['state'] == 'revoked':
            raise RuntimeAuthorizationDenied('text_analysis_reference_unavailable')
        changed = row['state'] == 'staged'
        if changed:
            db.execute("UPDATE text_analysis_authority_references SET state='active',version=version+1 WHERE reference_id=? AND state='staged'",
                       (reference.reference_id,))
            self.authentication._increment_authorization_epoch(db)
            evidence = replace(fence._evidence, reference_version=row['version'] + 1,
                authorization_epoch=self.authentication.read_epoch(db).value)
            current = replace(policy, authorization_epoch=self.authentication.read_epoch(db))
            fence._evidence, fence._epoch, fence._policy = evidence, current.authorization_epoch, current
        else:
            evidence, current = fence._evidence, policy
        self._check_current_in(db, current, context, evidence, phase='import')
        if not self.authentication.runtime_policy_is_current_in(db, current, now=self.authentication._clock()):
            raise RuntimeAuthorizationDenied('text_analysis_policy_stale')
        return TextAnalysisActivation(current, evidence, changed)

    @contextmanager
    def transition_fence(self, identity_policy: RuntimePolicy, context: TextAnalysisRunContext, *, kind: str,
                         phase: str, evidence: TextAnalysisCurrentEvidence | None = None,
                         lifecycle: FixtureTextAnalysisLifecycle | None = None, fixture_only: bool = False):
        from app.core.optional_execution import require_optional_execution
        require_optional_execution()
        if type(evidence) is not TextAnalysisCurrentEvidence or lifecycle is None:
            raise RuntimeAuthorizationDenied('text_analysis_current_evidence_missing')
        # Real seat/trust/activation and process-safe replacement providers are not installed.
        if (fixture_only is not True or evidence.fixture_only is not True
            or type(lifecycle) is not FixtureTextAnalysisLifecycle or lifecycle.fixture_only is not True
            or lifecycle.auth_database is not self.database):
            raise RuntimeAuthorizationDenied('text_analysis_production_provider_unavailable')
        with self.database.dependency_authority_gate:
            if self._schemas is None:
                self._schemas = TextAnalysisSchemas()
            with self.database.transaction() as db:
                if lifecycle.pair_owner is not None:
                    from app.persistence.authoritative_pair import AuthoritativePairOwner
                    pair = lifecycle.pair_owner
                    if (type(pair) is not AuthoritativePairOwner or pair.auth_database is not self.database
                            or pair.canonical_database is not lifecycle.canonical_database
                            or pair.installation_id != context.installation_id):
                        raise RuntimeAuthorizationDenied('authoritative_pair_unavailable')
                    pair.validate_auth_in(db)
                policy = self.compose_policy_in(db, identity_policy, context.reference_id, context,
                    trust=evidence.trust, schemas=self._schemas, kind=kind, now=self.authentication._clock())
                self._check_current_in(db, policy, context, evidence, phase=phase)
                fence = TextAnalysisTransitionFence(_SEAL, self, db, context, evidence, policy.authorization_epoch, lifecycle, phase)
                policy = replace(policy, text_analysis_fence=fence)
                fence._policy = policy
                try:
                    require_text_analysis(policy, context, kind=kind, phase=phase)
                    yield policy
                    self._check_current_in(db, fence._policy, context, fence._evidence, phase=phase)
                finally:
                    fence._active = False

    def _check_current_in(self, db, policy, context, evidence, *, phase):
        from app.persistence.nlp_dependencies import DependencyPersistence
        state = DependencyPersistence._authority(db)
        row = db.execute('SELECT state,version FROM text_analysis_authority_references WHERE reference_id=?', (context.reference_id,)).fetchone()
        profile = db.execute('SELECT closure_digest,state FROM nlp_dependency_profiles WHERE digest=?',
            (context.algorithm_profile_sha256,)).fetchone()
        if (row is None or row['state'] == 'revoked' or row['version'] != evidence.reference_version
            or (phase not in {'prepare', 'import'} and row['state'] != 'active')
            or profile is None or profile['state'] != 'available' or profile['closure_digest'] != context.algorithm_artifacts_sha256
            or policy.authorization_epoch.value != evidence.authorization_epoch
            or hashlib.sha256(encode_run_context(context)).hexdigest() != evidence.context_sha256
            or (context.seat_id, context.installation_key_id, context.installation_key_jkt) !=
               (evidence.seat_id, evidence.installation_key_id, evidence.installation_key_jkt)
            or context.activation_lineage_sha256 != evidence.activation_lineage_sha256
            or (state['epoch'], state['trust_sha256'], state['catalog_sha256']) !=
               (evidence.dependency_epoch, evidence.dependency_trust_sha256, evidence.dependency_catalog_sha256)
            or context.algorithm_profile_sha256 in state['withdrawals']):
            raise RuntimeAuthorizationDenied('text_analysis_current_evidence_changed')


def verification_context(context: TextAnalysisRunContext, kind: str) -> dict:
    value = {'expected_subject': f'organization:{context.organization_id}:installation:{context.installation_id}:seat:{context.seat_id}:capability:text-analysis',
        'expected_capability':'text-analysis', 'expected_target_major':context.licensed_major_version,
        'expected_artifact_family':context.artifact_family, 'requested_update_mode':context.activation_lineage_sha256 is not None,
        'expected_fallback_major':context.fallback_major_version}
    for name in ('organization_id','installation_id','installation_key_id','installation_key_jkt','seat_scope'):
        value['expected_' + name] = getattr(context, name)
    if kind == 'preview':
        from app.security.text_analysis_contract import OUTPUT_POLICY
        value['expected_output_policy'] = dict(OUTPUT_POLICY)
    return value
