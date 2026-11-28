"""Installed replacement reads use verified journals without hosted activity."""
import copy
import json

import pytest

from app.persistence.text_analysis_reissue import TextAnalysisReissueStore
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.security.text_analysis_contract import PREFIX, canonical
from app.security.text_analysis_reissue import (
    ReissueContext, validate_confirmation, validate_reissue_projection, validate_reissue_result,
    build_read_request, build_recover_request, build_finalize_request,
)
from tests.test_installed_text_analysis import registered, schemas, envelope, signed, delivery, digest
from tests.test_text_analysis_reissue import RESPONSES, NOW

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def adopted(owner, identity, signers, release_id, *, prior=None, seat='retained-reissue-seat', rights=True):
    value = copy.deepcopy(RESPONSES[0]['confirmation'])
    value.update(binding=identity.scope,release_identity=release_id)
    value['scope']['seat_id'] = seat
    value['scope']['update_rights'] = rights
    if prior:
        value.update(prior_license_id=prior.object_id,prior_issuance_id=prior.issuance_id)
        value['scope'] = {k: list(prior.claims[k]) if isinstance(prior.claims[k],tuple) else prior.claims[k] for k in value['scope']}
    original = {k:value[k] for k in ('profile','operation_id','binding','supported_profiles','release_identity')}
    ctx = ReissueContext(canonical(original),identity.key.public_key_jwk.encode())
    prepared = validate_reissue_projection(canonical(dict(profile=value['profile'],operation_id=value['operation_id'],binding=value['binding'],state='awaiting-authorization',expires_at='2026-10-26T00:30:00.000Z',entry_reference='A'*43)),operation='prepare',context=ctx,schemas=owner.schemas,now=NOW)
    confirm = validate_confirmation(canonical(value),context=prepared.context,schemas=owner.schemas)
    authorized = validate_reissue_projection(canonical(dict(profile=value['profile'],operation_id=value['operation_id'],binding=value['binding'],state='authorized',expires_at=confirm.expires_at,confirmation=value,confirmation_digest=confirm.confirmation_digest)),operation='read',context=prepared.context,schemas=owner.schemas,now=NOW,previous=prepared)
    inner = build_finalize_request(confirm,schemas=owner.schemas,now=NOW)
    result = copy.deepcopy(RESPONSES[0]['result'])
    result.update({k:value[k] for k in ('binding','prior_license_id','prior_issuance_id','authority_profile')})
    payload = json.loads(__import__('app.security.signed_objects',fromlist=['decode']).decode(result['signed_authority'].split('.')[1],16384))
    payload.update(identity.scope,**value['scope'])
    payload['sub'] = f"organization:{identity.scope['organization_id']}:installation:{identity.scope['installation_id']}:seat:{value['scope']['seat_id']}:capability:text-analysis"
    result.update(seat_id=value['scope']['seat_id'],finalization_request_digest=digest(inner),signed_authority=signed(canonical(payload),PREFIX+'license:v1',signers['license']).decode())
    context = {k:v for k,v in RESPONSES[0]['context'].items()}
    context.update({'expected_'+k:v for k,v in identity.scope.items()})
    context['expected_subject'] = payload['sub']
    context['requested_update_mode'] = False
    checked = validate_reissue_result(canonical(result),confirmation=confirm,original_finalization_request=inner,authority_context=context,trust=owner.release.trust,schemas=owner.schemas)
    store = TextAnalysisReissueStore(identity.auth,clock=lambda:NOW)
    r=store.begin(origin='https://authority.example',context=ctx,provider_name=identity.key.provider_name,provider_key_name=identity.key.provider_key_name,read_request=build_read_request(ctx,schemas=owner.schemas),recover_request=build_recover_request(ctx,schemas=owner.schemas))
    r=store.prepared(r,prepared); r=store.authorized(r,authorized); r=store.finalization(r,inner)
    return store.adopt(r,checked,authority_store=owner.authority,authority_context=context,trust=owner.release.trust,schemas=owner.schemas)


@pytest.mark.parametrize('initial',[False,True])
def test_adopted_reissue_is_read_without_write_and_retains_seat(tmp_path,schemas,initial,monkeypatch):
    with registered(tmp_path,schemas) as (owner,identity,signers):
        policy=lambda:identity.auth.read_bridge_session(identity.session.session_value).policy
        signature,body=envelope(owner.release,signers['release'])
        with identity.auth.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)',(owner.release.desktop_sha256,signature))
        owner.pair_owner.establish()
        prior=owner.adopt(policy(),**delivery('license',owner,identity,signers,body['release_identity'])) if initial else None
        r=adopted(owner,identity,signers,body['release_identity'],prior=prior)
        ref=owner.read_current(policy(),kind='license')
        assert ref.reference_id==r.metadata['reference_id']
        from app.persistence.authoritative_pair import audit_crosslinks
        from app.services import nlp_setup
        from types import SimpleNamespace
        monkeypatch.setattr(nlp_setup,'load_preparation_release',lambda:SimpleNamespace(execution=owner.release))
        with identity.auth.database.read() as db, owner.pair_owner.canonical_database.read() as c:
            audit_crosslinks(db,c,identity.scope['installation_id'])
        with owner._session(policy()) as (db,_,ident):
            assert owner.current_in(db,ident,'license').claims['seat_id']==ref.claims['seat_id']
            assert db.execute('SELECT total_changes()').fetchone()[0]==0
        with identity.auth.database.transaction() as db:
            db.execute("UPDATE text_analysis_authority_references SET state='revoked' WHERE reference_id=?",(ref.reference_id,))
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.read_current(policy(),kind='license')

def updated(owner):
    from dataclasses import replace
    package=json.loads(owner.release.package_binding)
    package.update(version='2.0.0.0',desktop_sha256='7'*64)
    pins=json.loads(owner.release.release_pins)
    pins.update(release_version='2.0.0.0',contract_manifest_sha256='8'*64,vector_manifest_sha256='9'*64)
    return replace(owner,release=replace(owner.release,desktop_sha256='7'*64,profile_sha256='6'*64,
                                       package_binding=canonical(package),release_pins=canonical(pins)))


def test_original_delivery_survives_compatible_core_update_and_exact_retry(tmp_path,schemas):
    from dataclasses import replace
    with registered(tmp_path,schemas) as (owner,identity,signers):
        policy=lambda:identity.auth.read_bridge_session(identity.session.session_value).policy
        signature,body=envelope(owner.release,signers['release'])
        with identity.auth.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)',(owner.release.desktop_sha256,signature))
        actual=delivery('license',owner,identity,signers,body['release_identity'])
        ref=owner.adopt(policy(),**actual)
        next_owner=updated(owner)
        assert next_owner.read_current(policy(),kind='license')==ref
        with next_owner._session(policy()) as (db,_,ident):
            assert next_owner.current_in(db,ident,'license',for_execution=True)==ref
        assert next_owner.adopt(policy(),**actual)==ref
        wrong=replace(policy(),identity=replace(policy().identity,creator_account_id='another-account'))
        with pytest.raises(RuntimeAuthorizationDenied):
            next_owner.read_current(wrong,kind='license')


def test_replacement_survives_update_but_conflicting_initial_ids_refuse(tmp_path,schemas):
    with registered(tmp_path,schemas) as (owner,identity,signers):
        policy=lambda:identity.auth.read_bridge_session(identity.session.session_value).policy
        signature,body=envelope(owner.release,signers['release'])
        with identity.auth.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)',(owner.release.desktop_sha256,signature))
        owner.pair_owner.establish()
        r=adopted(owner,identity,signers,body['release_identity'])
        next_owner=updated(owner)
        assert next_owner.read_current(policy(),kind='license').reference_id==r.metadata['reference_id']
        with next_owner._session(policy()) as (db,_,ident):
            assert next_owner.current_in(db,ident,'license',for_execution=True).reference_id==r.metadata['reference_id']
        owner.adopt(policy(),**delivery('license',owner,identity,signers,body['release_identity']))
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.read_current(policy(),kind='license')


def no_updates(actual,owner,signers):
    from dataclasses import replace
    from app.security.text_analysis_acquisition import validate_authorized_confirmation
    from app.security.signed_objects import decode
    confirmed=json.loads(actual['issued_from'].confirmation_bytes)
    confirmed['scope']['update_rights']=False
    trusted=validate_authorized_confirmation(canonical(confirmed),context=replace(actual['issued_from'].context,
        expected_scope=canonical(confirmed['scope'])),schemas=owner.schemas)
    request=json.loads(actual['original_issuance_request'])
    request['confirmation_digest']=trusted.confirmation_digest
    response=json.loads(actual['delivery'])
    payload=json.loads(decode(response['signed_authority'].split('.')[1],16384))
    payload['update_rights']=False
    response.update(issuance_request_digest=digest(canonical(request)),
                    signed_authority=signed(canonical(payload),PREFIX+'license:v1',signers['license']).decode())
    return dict(issued_from=trusted,original_issuance_request=canonical(request),delivery=canonical(response))


@pytest.mark.parametrize('replacement',[False,True])
def test_no_update_rights_preserves_status_and_recovery_but_denies_changed_execution(tmp_path,schemas,replacement):
    with registered(tmp_path,schemas) as (owner,identity,signers):
        policy=lambda:identity.auth.read_bridge_session(identity.session.session_value).policy
        signature,body=envelope(owner.release,signers['release'])
        with identity.auth.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)',(owner.release.desktop_sha256,signature))
        owner.pair_owner.establish()
        actual=None
        if replacement:
            adopted(owner,identity,signers,body['release_identity'],rights=False)
        else:
            actual=no_updates(delivery('license',owner,identity,signers,body['release_identity']),owner,signers)
            owner.adopt(policy(),**actual)
        with owner._session(policy()) as (db,_,ident):
            assert owner.current_in(db,ident,'license',for_execution=True) is not None
        next_owner=updated(owner)
        assert next_owner.read_current(policy(),kind='license') is not None
        if actual:
            assert next_owner.adopt(policy(),**actual) is not None
        with next_owner._session(policy()) as (db,_,ident):
            with pytest.raises(ValueError,match='update_rights_not_authorized'):
                next_owner.current_in(db,ident,'license',for_execution=True)

def retain_native_preview(owner,identity,actual,monkeypatch,*,fault=None):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from uuid import uuid4
    from app.persistence.text_analysis_native_acquisition import AcquisitionRecord,TextAnalysisAcquisitionStore
    from app.services import text_analysis_native_acquisition as native
    from app.security.text_analysis_acquisition import AcquisitionContext,NativeConsent,validate_acquisition_response,build_submit_request
    origin='https://provider.example'
    service=native.TextAnalysisNativeAcquisition(identity.auth,SimpleNamespace(transport=SimpleNamespace(origin=origin),schemas=owner.schemas),owner,
        select=lambda *_a,**_kw:None,eligible=lambda *_a:True,payment_mode='test')
    monkeypatch.setattr(native,'native_text_analysis_acquisition',lambda:service)
    confirmation=json.loads(actual['issued_from'].confirmation_bytes)
    prepare={k:confirmation[k] for k in ('operation_id','binding','authority_profile','supported_profiles','release_identity')}
    prepare['profile']=PREFIX+'preview-acquisition:v1'
    expiry=actual['issued_from'].context.expires_at
    ctx=AcquisitionContext(canonical(prepare),canonical(confirmation['scope']),'2026-10-25T00:30:00.000Z')
    at='2026-10-25T00:00:00.000Z'
    now=datetime(2026,10,25,tzinfo=timezone.utc)
    decision={'action':'acquire-text-analysis-preview','expires_at':expiry,
              'terms':{k:v for k,v in confirmation['scope'].items() if k not in ('seat_id','seat_scope')}}
    wire={k:prepare[k] for k in ('profile','operation_id','binding')}
    wire.update(state='confirmation-required',expires_at=expiry,confirmation=confirmation,
                confirmation_digest=digest(canonical(confirmation)),decision=decision,
                decision_digest=digest(canonical(decision)),decision_reference='A'*43)
    reviewed=validate_acquisition_response(canonical(wire),operation='read',context=ctx,schemas=owner.schemas,server_now=now)
    consent=NativeConsent(prepare['operation_id'],reviewed.confirmation_digest,reviewed.decision_digest,
                         reviewed.decision_reference,'acquire-text-analysis-preview')
    submit=build_submit_request(reviewed,native_consent=consent,schemas=owner.schemas,server_now=now)
    authorized={k:v for k,v in wire.items() if k not in ('decision','decision_digest','decision_reference')}
    authorized['state']='authorized'
    validate_acquisition_response(canonical(authorized),operation='submit',context=ctx,schemas=owner.schemas,server_now=now,previous=reviewed)
    scope=digest(canonical([origin,identity.identity.principal_id,identity.identity.creator_account_id,identity.scope]))
    data=dict(prepare=prepare,key=[identity.key.provider_name,identity.key.provider_key_name,identity.key.public_key_jwk],
        expected_scope=confirmation['scope'],operation_expires_at=ctx.operation_expires_at,
        decision=dict(body=canonical(wire).decode(),operation='read',at=at),
        authorized=dict(body=canonical(authorized).decode(),operation='submit',at=at),
        submit=submit.decode(),issuance=actual['original_issuance_request'].decode())
    if fault=='scope':scope='0'*64
    if fault=='issuance':data['issuance']=data['issuance']+' '
    if fault=='key':data['key'][1]='different-provider-key'
    if fault=='consent':data['submit']='{}'
    record=AcquisitionRecord(str(uuid4()),scope,'preview',prepare['operation_id'],1,at,data)
    if fault!='missing':
        TextAnalysisAcquisitionStore(identity.auth).begin(record,previous=None)
        if fault=='digest':
            with identity.auth.database.transaction() as db:
                db.execute('UPDATE text_analysis_native_acquisitions SET data_sha256=?',('0'*64,))
    return record


@pytest.mark.parametrize('fault',[None,'missing','scope','issuance','key','consent','digest'])
def test_original_native_issuance_recovers_before_first_adoption_after_core_update(tmp_path,schemas,monkeypatch,fault):
    with registered(tmp_path,schemas) as (owner,identity,signers):
        policy=lambda:identity.auth.read_bridge_session(identity.session.session_value).policy
        signature,body=envelope(owner.release,signers['release'])
        with identity.auth.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)',(owner.release.desktop_sha256,signature))
        actual=delivery('preview',owner,identity,signers,body['release_identity'])
        next_owner=updated(owner)
        retain_native_preview(next_owner,identity,actual,monkeypatch,fault=fault)
        if fault:
            with pytest.raises(ValueError):next_owner.adopt(policy(),**actual)
        else:
            ref=next_owner.adopt(policy(),**actual)
            assert next_owner.read_current(policy(),kind='preview')==ref
