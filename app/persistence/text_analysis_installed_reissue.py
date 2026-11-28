"""Read-only qualification of already adopted replacement licence journals."""
import json
from datetime import datetime, timezone

from app.persistence.text_analysis_reissue import TextAnalysisReissueStore
from app.security.text_analysis_contract import PREFIX, REISSUE_SUPPORTED, canonical, parse_json
from app.security.text_analysis_reissue import (
    ReissueContext, validate_confirmation, validate_context, validate_reissue_result,
    validate_reissue_projection, build_read_request, build_recover_request,
)
from app.persistence.text_analysis_installed import _need


def replacement_current_in(owner, db, identity, *, checked_references=None, for_execution=False):
    """Return one verified current-key successor; never prepare, adopt or write."""
    binding = {name: getattr(identity, name) for name in (
        'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
    rows = db.execute("SELECT * FROM text_analysis_reissue_operations WHERE state='adopted' LIMIT 65").fetchall()
    _need(len(rows) <= 64)
    records = [TextAnalysisReissueStore._row(db, row) for row in rows]
    selected = [r for r in records if r.metadata['binding'] == binding]
    if not selected:
        return None
    key = owner.authority.authentication.installation_key_reference(connection=db)
    _need(key is not None)
    verified = []
    for record in selected:
        m, b = record.metadata, record.bodies
        _need((m['provider_name'], m['provider_key_name']) == (key.provider_name, key.provider_key_name)
              and canonical(json.loads(m['public_key_jwk'])) == canonical(json.loads(key.public_key_jwk)))
        ctx = validate_context(ReissueContext(b['prepare_request'], m['public_key_jwk'].encode(),
                                             m['operation_expires_at']), schemas=owner.schemas)
        original = parse_json(ctx.original_prepare, maximum=8192, require_canonical=True)
        _need(ctx.binding == binding and ctx.operation_id == m['operation_id']
              and owner.retained_release_identity_in(db,canonical(original['release_identity'])) == canonical(original['release_identity'])
              and original['supported_profiles']['profiles'] == list(REISSUE_SUPPORTED)
              and build_read_request(ctx, schemas=owner.schemas) == b['read_request']
              and build_recover_request(ctx, schemas=owner.schemas) == b['recover_request'])
        confirmation = validate_confirmation(b['confirmation'], context=ctx, schemas=owner.schemas)
        value = json.loads(confirmation.confirmation_bytes)
        scope = value['scope']
        _need(value['authority_profile'] == PREFIX+'license:v1'
              and confirmation.expires_at == m['confirmation_expires_at']
              and scope['seat_scope'] == 'organization-installation-seat'
              and scope['capability'] == 'text-analysis' and scope['licensed_major_version'] == 1
              and scope['compatible_artifact_family'] == 'text-analysis-artifact')
        context = {'expected_'+name: val for name, val in binding.items()}
        context.update(expected_subject=f"organization:{identity.organization_id}:installation:{identity.installation_id}:seat:{scope['seat_id']}:capability:text-analysis",
            expected_seat_scope='organization-installation-seat', expected_capability='text-analysis',
            expected_target_major=1, expected_artifact_family='text-analysis-artifact',
            requested_update_mode=False, expected_fallback_major=None)
        # Adoption is complete: replay consistency at the retained deadline, not a new creation window.
        replay_time = datetime.strptime(confirmation.expires_at,'%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        previous = validate_reissue_projection(b['prepare_response'], operation='prepare', context=ctx,
            schemas=owner.schemas, now=replay_time)
        authorized = validate_reissue_projection(b['authorized_response'], operation='read', context=ctx,
            schemas=owner.schemas, now=replay_time, previous=previous)
        _need(authorized.confirmation == confirmation)
        result = validate_reissue_result(b['result'], confirmation=confirmation,
            original_finalization_request=b['finalize_request'], authority_context=context,
            trust=owner.release.trust, schemas=owner.schemas, previous_response=b['result'])
        from app.security.text_analysis_references import reference_from_verified
        ref = reference_from_verified(result.authority)
        stored = db.execute('SELECT * FROM text_analysis_authority_references WHERE reference_id=?',
                            (m['reference_id'],)).fetchone()
        _need(stored is not None and ref.reference_id == m['reference_id'])
        owner.authority._compare_row(stored,ref)
        _need(not ref.fixture_only and ref.compact_jws == result.authority.compact_jws)
        verified.append((ref, context, value))
    # IDs define the successor relation; timestamps cannot resolve conflicting grants.
    successors = {(v['prior_license_id'], v['prior_issuance_id']) for _, _, v in verified}
    _need(len(successors) == len(verified))
    tips = [(r,c,v) for r,c,v in verified if (r.object_id,r.issuance_id) not in successors]
    _need(len(tips) == 1)
    tip = tips[0]
    seen = set()
    current = tip
    while True:
        ref, _, value = current
        _need(ref.reference_id not in seen)
        seen.add(ref.reference_id)
        prior = (value['prior_license_id'], value['prior_issuance_id'])
        matches = [(r,c,v) for r,c,v in verified if (r.object_id,r.issuance_id) == prior]
        if not matches:
            break
        _need(len(matches) == 1 and matches[0][2]['scope'] == value['scope'])
        current = matches[0]
    _need(len(seen) == len(verified))
    # Any locally retained predecessor must agree exactly with the preserved seat/rights.
    prior_rows = db.execute("SELECT * FROM text_analysis_authority_references WHERE kind='license' AND (object_id=? OR issuance_id=?)", prior).fetchall()
    predecessor = None
    if prior_rows:
        _need(len(prior_rows) == 1)
        row = prior_rows[0]
        _need((row['object_id'],row['issuance_id']) == prior)
        from app.security.signed_objects import decode
        claims = parse_json(decode(row['compact_jws'].split('.')[1],16384),maximum=16384,require_canonical=True)
        prior_context = dict(current[1])
        for name in binding:
            prior_context['expected_'+name] = claims[name]
        prior_context['expected_subject'] = f"organization:{claims['organization_id']}:installation:{claims['installation_id']}:seat:{current[2]['scope']['seat_id']}:capability:text-analysis"
        from app.security.text_analysis_authority import verify_authority
        from app.security.text_analysis_references import reference_from_verified
        decision = verify_authority(row['compact_jws'],kind='license',context=prior_context,
                                    trust=owner.release.trust,schemas=owner.schemas)
        _need(decision.accepted)
        predecessor = reference_from_verified(decision.authority)
        owner.authority._compare_row(row,predecessor)
        _need(claims['organization_id'] == identity.organization_id
              and all(canonical(claims[k]) == canonical(v) for k,v in current[2]['scope'].items())
              and not predecessor.fixture_only)
    if for_execution:
        tip[1]['requested_update_mode'] = owner.applies_update_in(db,canonical(tip[2]['release_identity']))
    owner.authority.get_reference_in(db,tip[0].reference_id,context=tip[1],
                                    trust=owner.release.trust,schemas=owner.schemas)
    if checked_references is not None:
        checked_references.update({ref.reference_id: ref for ref, _, _ in verified})
        if predecessor is not None:
            checked_references[predecessor.reference_id] = predecessor
    return tip[0], tip[1], prior
