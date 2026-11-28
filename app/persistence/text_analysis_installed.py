"""Verify retained release and text-analysis authority records in the owning databases."""
from dataclasses import dataclass
import hashlib
from typing import ClassVar

from app.nlp.execution_release import ExecutionRelease
from app.persistence.authoritative_pair import AuthoritativePairOwner, RuntimeRootLease
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.security.signed_objects import encode
from app.security.text_analysis_contract import OUTPUT_POLICY, SUPPORTED, TextAnalysisSchemas, canonical, parse_json
from app.security.text_analysis_delivery import validate_delivery_response
from app.security.text_analysis_identity import _scope
from app.security.text_analysis_references import _plain, reference_from_verified


def _need(ok):
    if not ok:
        raise RuntimeAuthorizationDenied('text_analysis_delivery_unavailable')

def _read(raw, limit):
    return parse_json(raw, maximum=limit, require_canonical=True)

def expected_installation_seat(organization_id, installation_id):
    parts = ('capability-license-seat', organization_id, installation_id, 'text-analysis')
    return 'id-' + encode(hashlib.sha256(chr(31).join(parts).encode('utf-8')).digest()[:16])

def _expected(identity, confirmation, kind):
    expected_binding = {field: getattr(identity, field) for field in (
        'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
    scope = confirmation['scope']
    _need(scope['seat_id'] == expected_installation_seat(identity.organization_id, identity.installation_id)
          and confirmation['binding'] == expected_binding
          and scope['seat_scope'] == 'organization-installation-seat'
          and scope['capability'] == 'text-analysis' and scope['licensed_major_version'] == 1
          and scope['compatible_artifact_family'] == 'text-analysis-artifact')
    seat = scope['seat_id']
    context = {'expected_' + field: value for field, value in expected_binding.items()}
    context.update(expected_subject=f'organization:{identity.organization_id}:installation:{identity.installation_id}:seat:{seat}:capability:text-analysis',
        expected_seat_scope=scope['seat_scope'], expected_capability='text-analysis', expected_target_major=1,
        expected_artifact_family='text-analysis-artifact', requested_update_mode=False, expected_fallback_major=None)
    if kind == 'preview':
        context['expected_output_policy'] = dict(OUTPUT_POLICY)
    return context


@dataclass(frozen=True, slots=True)
class InstalledTextAnalysisRecords:
    release: ExecutionRelease
    authority: TextAnalysisAuthorityStore
    pair_owner: AuthoritativePairOwner
    schemas: TextAnalysisSchemas
    fixture_only: ClassVar[bool] = False

    def __post_init__(self):
        _need(type(self.release) is ExecutionRelease and self.release.trust.fixture_only is False
              and type(self.authority) is TextAnalysisAuthorityStore and type(self.schemas) is TextAnalysisSchemas
              and type(self.pair_owner) is AuthoritativePairOwner and type(self.pair_owner.root_lease) is RuntimeRootLease
              and self.pair_owner.authentication is self.authority.authentication)

    def release_identity_in(self, db):
        _need(self.authority.database.owns_connection(db) and db.in_transaction)
        row = db.execute('SELECT envelope FROM text_analysis_release_identities WHERE desktop_sha256=?',
                         (self.release.desktop_sha256,)).fetchone()
        return self.release.verify_identity(row['envelope'],self.schemas) if row else None

    def _retained_release_in(self, db, expected):
        """Authenticate the original release without requiring it to be today's package."""
        _need(self.authority.database.owns_connection(db) and db.in_transaction)
        from app.security.signed_objects import decode
        matches = []
        for row in db.execute('SELECT desktop_sha256,envelope FROM text_analysis_release_identities'):
            envelope = bytes(row['envelope'])
            _need(len(envelope) <= 24576)
            body = parse_json(decode(envelope.decode('ascii').split('.')[1],16384),maximum=16384,require_canonical=True)
            if canonical(body['release_identity']) == expected:
                matches.append((self.release.verify_retained_identity(envelope,self.schemas,
                                                                    desktop_sha256=row['desktop_sha256']),row['desktop_sha256']))
        _need(len(matches) == 1 and matches[0][0] == expected)
        return matches[0]

    def retained_release_identity_in(self, db, expected):
        return self._retained_release_in(db,expected)[0]

    def applies_update_in(self, db, expected):
        return self._retained_release_in(db,expected)[1] != self.release.desktop_sha256

    def _delivery(self, db, identity, *, kind, confirmation, issuance, response, retained=False, for_execution=False):
        _need(kind in ('license','preview'))
        confirmed = _read(confirmation, 4096)
        original = _read(issuance, 8192)
        self.schemas.require_definition('schemas/text-analysis-authority-issuance/v1/definitions.schema.json',
                                        kind + 'Confirmation', confirmed)
        _need(confirmed['authority_profile'] == 'urn:bridge-clean:text-analysis-' + kind + ':v1'
              and confirmed['supported_profiles']['profiles'] == list(SUPPORTED)
              and canonical(confirmed['release_identity']) == (self.retained_release_identity_in(db,canonical(confirmed['release_identity'])) if retained else self.release_identity_in(db))
              and original['confirmation_digest'] == hashlib.sha256(confirmation).hexdigest()
              and all(original[name] == confirmed[name] for name in (
                  'operation_id','issuance_authorization_id','binding','authority_profile','supported_profiles','release_identity')))
        context = _expected(identity, confirmed, kind)
        if for_execution and kind == 'license':
            context['requested_update_mode'] = self.applies_update_in(db,canonical(confirmed['release_identity']))
        result = validate_delivery_response(response, original_issuance_request=issuance,
            expected_seat_id=confirmed['scope']['seat_id'], authority_context=context,
            trust=self.release.trust, schemas=self.schemas)
        ref = reference_from_verified(result.authority)
        _need(not ref.fixture_only and ref.kind == kind
              and all(canonical(_plain(ref.claims[k])) == canonical(v) for k,v in confirmed['scope'].items()))
        return ref, context

    def current_in(self, db, identity, kind, *, for_execution=False):
        _need(kind in ('license','preview') and self.authority.database.owns_connection(db) and db.in_transaction)
        row = db.execute('SELECT * FROM text_analysis_deliveries WHERE principal_id=? AND creator_account_id=? AND kind=?',
                         (identity.principal_id, identity.creator_account_id, kind)).fetchone()
        replacement = None
        if kind == 'license':
            from app.persistence.text_analysis_installed_reissue import replacement_current_in
            replacement = replacement_current_in(self, db, identity, for_execution=for_execution)
        if row is None:
            return replacement[0] if replacement else None
        delivery_identity = identity
        if replacement:
            original = _read(row['confirmation'],4096)
            delivery_identity = identity.model_copy(update=original['binding'])
        ref, context = self._delivery(db,delivery_identity,kind=kind,confirmation=row['confirmation'],
                                      issuance=row['issuance_request'],response=row['response'],retained=True,for_execution=for_execution and not replacement)
        _need(ref.reference_id == row['reference_id'] and _read(row['issuance_request'],8192)['operation_id']==row['operation_id'])
        if kind == 'license' and not replacement:
            from app.persistence.text_analysis_reissue import TextAnalysisReissueStore
            for journal in db.execute("SELECT * FROM text_analysis_reissue_operations WHERE state='adopted'"):
                record = TextAnalysisReissueStore._row(db,journal)
                prior = _read(record.bodies['confirmation'],8192)
                _need((prior['prior_license_id'],prior['prior_issuance_id']) != (ref.object_id,ref.issuance_id))
        if replacement:
            _need((ref.object_id,ref.issuance_id) == replacement[2])
            return replacement[0]
        return self.authority.get_reference_in(db,ref.reference_id,context=context,trust=self.release.trust,schemas=self.schemas)

    @property
    def auth_database(self):
        return self.pair_owner.auth_database

    @property
    def canonical_database(self):
        return self.pair_owner.canonical_database

    def validate(self, db, policy, context, kind):
        from app.core.optional_execution import installed_execution_release

        _need(self.release == installed_execution_release()
              and context.algorithm_profile_sha256 == self.release.profile_sha256
              and context.installation_id == self.pair_owner.installation_id)
        reference = self.current_in(db, _scope(policy), kind, for_execution=True)
        _need(reference is not None and reference.reference_id == context.reference_id
              and reference.compact_jws_sha256 == context.compact_jws_sha256
              and context.seat_id == reference.claims['seat_id'])
