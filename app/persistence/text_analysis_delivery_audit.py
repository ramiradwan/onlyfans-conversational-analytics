"""Offline integrity verification of retained, package-qualified deliveries.

This checks historical evidence, not current execution admission. Expired or
revoked sessions/grants remain evidence of the original account binding.
"""
from types import SimpleNamespace
import json

from app.security.runtime_policy import RuntimeAuthorizationDenied


def _need(value):
    if not value:
        raise RuntimeAuthorizationDenied('authoritative_pair_unavailable')


def _identity(db, delivery, installation_id, *, expected_binding=None):
    from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
    key = db.execute('SELECT * FROM installation_key_reference WHERE singleton=1').fetchone()
    _need(key is not None and key['activated_at'] is not None)
    sessions = db.execute('SELECT s.session_id FROM bridge_sessions s JOIN webauthn_credentials c '
        'ON c.credential_id=s.credential_id WHERE s.principal_id=? AND s.creator_account_id=? '
        'AND c.principal_id=s.principal_id AND c.installation_id=?',
        (delivery['principal_id'], delivery['creator_account_id'], installation_id)).fetchall()
    candidates = set()
    for session in sessions:
        grants = db.execute('SELECT g.* FROM verified_grant_references g JOIN bridge_session_grants s '
            'ON s.grant_reference_id=g.reference_id WHERE s.session_id=?', (session['session_id'],)).fetchall()
        grants = [g for g in grants if g['grant_type'] in ACCOUNT_AUTHORITY_GRANT_TYPES]
        if not set(ACCOUNT_AUTHORITY_GRANT_TYPES).issubset(g['grant_type'] for g in grants):
            continue
        bindings = {(g['organization_id'], g['installation_id'], g['installation_key_id'],
                     g['installation_key_jkt']) for g in grants}
        if len(bindings) != 1:
            continue
        binding = next(iter(bindings))
        if (None in binding or binding[1] != installation_id
                or any(g['creator_account_id'] not in (None, delivery['creator_account_id']) for g in grants)
                or any(g['allowed_creator_account_ids'] is not None and delivery['creator_account_id']
                       not in json.loads(g['allowed_creator_account_ids']) for g in grants)):
            continue
        if expected_binding is not None and binding != tuple(expected_binding[k] for k in (
                'organization_id','installation_id','installation_key_id','installation_key_jkt')):
            continue
        candidates.add(binding)
    _need(len(candidates) == 1)
    organization, installation, key_id, key_jkt = next(iter(candidates))
    if (key_id, key_jkt) != (key['installation_key_id'], key['installation_key_jkt']):
        _need(db.execute("SELECT 1 FROM text_analysis_reissue_operations WHERE state='adopted' LIMIT 1").fetchone())
    return SimpleNamespace(organization_id=organization, installation_id=installation,
        installation_key_id=key_id, installation_key_jkt=key_jkt)


def retained_release_identity(db, release, schemas, expected):
    """Qualify an original release using installed, purpose-separated trust.

    Compatible later Core versions may retain an earlier signed release. The
    original package family and independent authority trust remain pinned.
    """
    from app.security.text_analysis_contract import canonical
    matches = []
    for row in db.execute('SELECT desktop_sha256,envelope FROM text_analysis_release_identities'):
        actual = release.verify_retained_identity(row['envelope'], schemas, desktop_sha256=row['desktop_sha256'])
        if actual == canonical(expected):
            matches.append(actual)
    _need(len(matches) == 1)
    return matches[0]


def audit_installed_deliveries(db, installation_id, schemas):
    """Use installed public trust and retained delivery basis, never token context."""
    from app.nlp.execution_release import ExecutionRelease
    from app.nlp.preparation import load_preparation_release
    from app.persistence.text_analysis_installed import InstalledTextAnalysisRecords
    admitted = load_preparation_release()
    release = getattr(admitted, 'execution', None)
    _need(type(release) is ExecutionRelease and release.trust.fixture_only is False)
    # Reuse exactly the adoption validator without creating an owner, store or
    # session, or writing to either authoritative database.
    references = {}
    for delivery in db.execute('SELECT * FROM text_analysis_deliveries'):
        from app.security.text_analysis_contract import parse_json
        original = parse_json(delivery['issuance_request'], maximum=8192, require_canonical=True)
        release_identity = retained_release_identity(db, release, schemas, original['release_identity'])
        validator = SimpleNamespace(release=release, schemas=schemas,
            release_identity_in=lambda _db: release_identity,
            retained_release_identity_in=lambda _db, expected: retained_release_identity(
                _db, release, schemas, json.loads(expected)))
        identity = _identity(db, delivery, installation_id, expected_binding=original['binding'])
        ref, _ = InstalledTextAnalysisRecords._delivery(validator, db, identity,
            kind=delivery['kind'], confirmation=delivery['confirmation'],
            issuance=delivery['issuance_request'], response=delivery['response'], retained=True)
        _need(ref.reference_id == delivery['reference_id'] and original['operation_id'] == delivery['operation_id']
              and ref.reference_id not in references)
        references[ref.reference_id] = ref
    _audit_replacements(db, installation_id, release, schemas, references)
    for ref in references.values():
        binding = {name: ref.claims[name] for name in (
            'organization_id','installation_id','installation_key_id','installation_key_jkt')}
        matches = []
        for session in db.execute('SELECT DISTINCT principal_id,creator_account_id FROM bridge_sessions'):
            try:
                matches.append(_identity(db, session, installation_id, expected_binding=binding))
            except RuntimeAuthorizationDenied:
                continue
        _need(matches)
    return references


def _audit_replacements(db, installation_id, release, schemas, references):
    from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
    from app.persistence.text_analysis_installed_reissue import replacement_current_in
    from app.security.text_analysis_authority import verify_authority
    from app.security.text_analysis_references import reference_from_verified
    key_row = db.execute('SELECT * FROM installation_key_reference WHERE singleton=1').fetchone()
    key = SimpleNamespace(**dict(key_row)) if key_row else None

    def get_reference(connection, reference_id, *, context, trust, schemas):
        row = connection.execute('SELECT * FROM text_analysis_authority_references WHERE reference_id=?', (reference_id,)).fetchone()
        _need(row is not None)
        decision = verify_authority(row['compact_jws'], kind=row['kind'], context=context, trust=trust, schemas=schemas)
        _need(decision.accepted)
        ref = reference_from_verified(decision.authority)
        TextAnalysisAuthorityStore._compare_row(row, ref)
        _need(ref.reference_id == reference_id)
        return ref

    authority = SimpleNamespace(authentication=SimpleNamespace(installation_key_reference=lambda **kw: key),
        get_reference_in=get_reference, _compare_row=TextAnalysisAuthorityStore._compare_row)
    facade = SimpleNamespace(release=release, schemas=schemas, authority=authority,
        retained_release_identity_in=lambda _db, expected: retained_release_identity(
            _db, release, schemas, json.loads(expected)))
    for row in db.execute("SELECT DISTINCT metadata FROM text_analysis_reissue_operations WHERE state='adopted'"):
        metadata = json.loads(row['metadata'])
        binding = metadata['binding']
        _need(binding['installation_id'] == installation_id)
        # The mature reissue journal is installation scoped. Require independent
        # retained account/session grant evidence of its original binding.
        identities = []
        for session in db.execute('SELECT DISTINCT principal_id,creator_account_id FROM bridge_sessions'):
            try:
                identities.append(_identity(db, session, installation_id, expected_binding=binding))
            except RuntimeAuthorizationDenied:
                continue
        _need(identities)
        replacement_current_in(facade, db, identities[0], checked_references=references)
