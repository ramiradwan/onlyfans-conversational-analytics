"""Retain and recheck exact module deliveries for a signed-in desktop account."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import time

from app.nlp.execution_release import ExecutionRelease
from app.persistence.authoritative_pair import AuthoritativePairOwner, RuntimeRootLease
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.security.signed_objects import encode
from app.security.text_analysis_acquisition import AuthorizedConfirmation, validate_authorized_confirmation
from app.security.text_analysis_contract import OUTPUT_POLICY, SUPPORTED, TextAnalysisSchemas, canonical, parse_json
from app.security.text_analysis_delivery import validate_delivery_response
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
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
class InstalledTextAnalysisAuthorities:
    release: ExecutionRelease
    authority: TextAnalysisAuthorityStore
    pair_owner: AuthoritativePairOwner
    schemas: TextAnalysisSchemas

    def __post_init__(self):
        _need(type(self.release) is ExecutionRelease and self.release.trust.fixture_only is False
              and type(self.authority) is TextAnalysisAuthorityStore and type(self.schemas) is TextAnalysisSchemas
              and type(self.pair_owner) is AuthoritativePairOwner and type(self.pair_owner.root_lease) is RuntimeRootLease
              and self.pair_owner.authentication is self.authority.authentication)

    @contextmanager
    def _session(self, policy, *, write=False):
        auth = self.authority.authentication
        with auth.database.dependency_authority_gate:
            identity = TextAnalysisIdentityOwner(auth).capture(policy)
            _need(identity.installation_id == self.pair_owner.installation_id)
            with auth.database.transaction() as db, self.pair_owner.canonical_database.read() as canonical_db:
                canonical_db.execute('BEGIN')
                def check_pair():
                    left = db.execute('SELECT 1 FROM authoritative_pair').fetchone()
                    right = canonical_db.execute('SELECT 1 FROM authoritative_pair').fetchone()
                    if left or right:
                        self.pair_owner.validate_read_in(db, canonical_db)
                    else:
                        self.pair_owner.validate_auth_in(db)
                        _need(not write and not db.execute('SELECT 1 FROM text_analysis_authority_references LIMIT 1').fetchone()
                              and not db.execute('SELECT 1 FROM text_analysis_deliveries LIMIT 1').fetchone()
                              and not canonical_db.execute('SELECT 1 FROM text_analysis_preview_admissions LIMIT 1').fetchone())
                check_pair()
                def current():
                    session = auth._require_session_current(db, policy.identity.session_id, auth._clock())
                    key = auth.installation_key_reference(connection=db)
                    _need(key and (key.installation_key_id, key.installation_key_jkt) ==
                          (identity.installation_key_id, identity.installation_key_jkt)
                          and (session['principal_id'],session['creator_account_id'],session['role']) ==
                          (identity.principal_id,identity.creator_account_id,identity.role)
                          and set(auth._session_grants(db, policy.identity.session_id)) == set(identity.grant_reference_ids))
                current()
                yield db, canonical_db, identity
                current()
                check_pair()

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

    def acquisition_basis(self, policy, *, refresh=False):
        """Read the retained release; only an explicit acquisition can fetch its signed descriptor."""
        with self._session(policy) as (db, _, identity):
            raw = self.release_identity_in(db)
        if raw is None and refresh:
            from app.services.nlp_setup import load_preparation_release
            from app.services.nlp_distribution import ProfileDistribution
            admitted = load_preparation_release()
            _need(admitted and admitted.execution and admitted.execution.desktop_sha256 == self.release.desktop_sha256)
            transport = ProfileDistribution(admitted.distribution_base_url)
            envelope = transport.read('releases/'+self.release.desktop_sha256,24576,
                                      check=lambda: None,deadline=time.monotonic()+20)
            raw = self.release.verify_identity(envelope,self.schemas)
            with self._session(policy) as (db, _, current):
                _need(current == identity)
                db.execute('INSERT OR IGNORE INTO text_analysis_release_identities VALUES(?,?)',
                           (self.release.desktop_sha256,envelope))
                _need(raw == self.release_identity_in(db))
        return raw, expected_installation_seat(identity.organization_id, identity.installation_id)

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

    @staticmethod
    def _same_delivery_in(db, identity, kind, confirmation, issuance, response):
        operation = _read(issuance,8192)['operation_id']
        row = db.execute('SELECT * FROM text_analysis_deliveries WHERE operation_id=?',(operation,)).fetchone()
        return row is not None and (row['principal_id'],row['creator_account_id'],row['kind'],
            bytes(row['confirmation']),bytes(row['issuance_request']),bytes(row['response'])) == (
            identity.principal_id,identity.creator_account_id,kind,confirmation,issuance,response)

    def _retained_native_issuance_in(self, db, identity, kind, confirmation, issuance):
        """Only a persisted native operation can recover D before first adoption."""
        from app.persistence.text_analysis_native_acquisition import _decode
        from app.services.text_analysis_native_acquisition import native_text_analysis_acquisition, TextAnalysisNativeAcquisition
        from app.security.text_analysis_acquisition import acquisition_confirmation, NativeConsent, build_submit_request
        protocol_kind = 'paid' if kind == 'license' else 'preview'
        request = _read(issuance,8192)
        rows = db.execute('SELECT * FROM text_analysis_native_acquisitions WHERE operation_id=?',
                          (request['operation_id'],)).fetchall()
        if not rows:
            return False
        _need(len(rows) == 1)
        record = _decode(rows[0])
        service = native_text_analysis_acquisition()
        _need(type(service) is TextAnalysisNativeAcquisition and service.authorities is self
              and service.authentication is self.authority.authentication)
        binding = {name:getattr(identity,name) for name in (
            'organization_id','installation_id','installation_key_id','installation_key_jkt')}
        scope = hashlib.sha256(canonical([service.origin,identity.principal_id,identity.creator_account_id,binding])).hexdigest()
        key = self.authority.authentication.installation_key_reference(connection=db)
        _need(key is not None and record.kind == protocol_kind and record.scope == scope
              and record.data.get('issuance','').encode('utf-8') == issuance
              and record.data['prepare']['binding'] == binding
              and record.data['prepare']['authority_profile'] == 'urn:bridge-clean:text-analysis-'+kind+':v1'
              and record.data['prepare']['profile'] == 'urn:bridge-clean:text-analysis-'+protocol_kind+'-acquisition:v1'
              and record.data['key'] == [key.provider_name,key.provider_key_name,key.public_key_jwk])
        _, reviewed, authorized, _ = service._chain(record)
        _need(reviewed is not None and authorized is not None)
        consent = NativeConsent(record.operation_id,reviewed.confirmation_digest,reviewed.decision_digest,
            reviewed.decision_reference,'activate-paid-text-analysis' if protocol_kind == 'paid' else 'acquire-text-analysis-preview')
        from datetime import datetime, timezone
        observed_at = datetime.strptime(record.data['decision']['at'],'%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        submitted = build_submit_request(reviewed,native_consent=consent,schemas=self.schemas,server_now=observed_at)
        _need(record.data.get('submit','').encode('utf-8') == submitted)
        _need(authorized is not None and acquisition_confirmation(authorized,schemas=self.schemas).confirmation_bytes == confirmation)
        return True

    def _original_delivery_in(self, db, identity, kind, confirmation, issuance, response):
        return (self._same_delivery_in(db,identity,kind,confirmation,issuance,response)
                or self._retained_native_issuance_in(db,identity,kind,confirmation,issuance))

    def adopt(self, policy, *, issued_from, original_issuance_request, delivery):
        """Receive the retained protected confirmation from the native issuance owner."""
        _need(type(issued_from) is AuthorizedConfirmation)
        checked = validate_authorized_confirmation(issued_from.confirmation_bytes, context=issued_from.context, schemas=self.schemas)
        _need(checked == issued_from)
        kind = checked.context.authority_kind
        with self.authority.database.dependency_authority_gate:
            with self._session(policy) as (db, _, identity):
                self._delivery(db,identity,kind=kind,confirmation=checked.confirmation_bytes,
                               issuance=original_issuance_request,response=delivery,
                               retained=self._original_delivery_in(db,identity,kind,checked.confirmation_bytes,original_issuance_request,delivery))
                paired = db.execute('SELECT 1 FROM authoritative_pair').fetchone()
            if not paired:
                self.pair_owner.establish()
        with self._session(policy, write=True) as (db, _, identity):
            ref, context = self._delivery(db, identity, kind=kind, confirmation=checked.confirmation_bytes,
                                         issuance=original_issuance_request, response=delivery,
                                         retained=self._original_delivery_in(db,identity,kind,checked.confirmation_bytes,original_issuance_request,delivery))
            values = (ref.reference_id, checked.context.operation_id, identity.principal_id, identity.creator_account_id,
                      kind, checked.confirmation_bytes, original_issuance_request, delivery)
            row = db.execute('SELECT * FROM text_analysis_deliveries WHERE reference_id=? OR operation_id=? '
                'OR (principal_id=? AND creator_account_id=? AND kind=?)', values[:5]).fetchall()
            if row:
                _need(len(row) == 1 and tuple(row[0]) == values)
                return self.authority.get_reference_in(db, ref.reference_id, context=context,
                                                     trust=self.release.trust, schemas=self.schemas)
            self.authority.import_verified_reference_in(db,ref.compact_jws,kind=kind,context=context,
                                                       trust=self.release.trust, schemas=self.schemas)
            db.execute('INSERT INTO text_analysis_deliveries VALUES(?,?,?,?,?,?,?,?)',values)
            return ref

    def current_in(self, db, identity, kind, *, for_execution=False):
        _need(kind in ('license','preview') and self.authority.database.owns_connection(db) and db.in_transaction)
        row = db.execute('SELECT * FROM text_analysis_deliveries WHERE principal_id=? AND creator_account_id=? AND kind=?',
                         (identity.principal_id, identity.creator_account_id, kind)).fetchone()
        replacement = None
        if kind == 'license':
            from app.services.installed_text_analysis_reissue import replacement_current_in
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

    def read_current(self, policy, *, kind):
        with self._session(policy) as (db, _, identity):
            return self.current_in(db,identity,kind)

    def preview_state(self, policy):
        with self._session(policy) as (db, canonical_db, identity):
            ref = self.current_in(db,identity,'preview')
            if ref is None:
                return None
            admission = canonical_db.execute('SELECT * FROM text_analysis_preview_admissions WHERE installation_id=? AND permit_id=?',
                                             (identity.installation_id, ref.object_id)).fetchone()
            if admission is None:
                staged = db.execute('SELECT state FROM text_analysis_authority_references WHERE reference_id=?',
                                    (ref.reference_id,)).fetchone()
                _need(staged is not None and staged['state'] == 'staged')
                return 'available'
            _need((admission['reference_id'], admission['compact_jws_sha256'], admission['issuance_id']) ==
                  (ref.reference_id, ref.compact_jws_sha256, ref.issuance_id))
            return admission['state']


def installed_text_analysis_authorities():
    from app.services.text_analysis_preview import preview_service
    service = preview_service()
    owner = getattr(getattr(service, 'selection', None), 'installed_authorities', None)
    return owner if type(owner) is InstalledTextAnalysisAuthorities else None
