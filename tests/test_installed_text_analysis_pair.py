"""Matched-store audits retain actual installed delivery trust and lineage."""
from types import SimpleNamespace
from dataclasses import replace
import json

import pytest

from app.persistence.authoritative_pair import audit_crosslinks
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
from app.nlp import preparation
from tests.test_installed_text_analysis import registered, envelope, delivery

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def audit(owner):
    with owner.authority.database.read() as auth, owner.pair_owner.canonical_database.read() as canonical_db:
        before = auth.total_changes, canonical_db.total_changes
        audit_crosslinks(auth, canonical_db, owner.pair_owner.installation_id)
        assert before == (auth.total_changes, canonical_db.total_changes)


@pytest.mark.parametrize('kind', ['license', 'preview'])
def test_real_delivery_survives_repeated_matched_audit(tmp_path, monkeypatch, kind):
    with registered(tmp_path, TextAnalysisSchemas()) as (owner, identity, signers):
        monkeypatch.setattr(preparation, 'load_preparation_release', lambda: SimpleNamespace(execution=owner.release))
        signature, body = envelope(owner.release, signers['release'])
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, signature))
        owner.adopt(identity.auth.read_bridge_session(identity.session.session_value).policy, **delivery(kind, owner, identity, signers, body['release_identity']))
        audit(owner)
        audit(owner)
        assert owner.pair_owner.establish() == owner.pair_owner.establish()


def test_real_delivery_audit_accepts_authenticated_prior_compatible_core(tmp_path, monkeypatch):
    with registered(tmp_path, TextAnalysisSchemas()) as (owner, identity, signers):
        signature, body = envelope(owner.release, signers['release'])
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, signature))
        owner.adopt(identity.auth.read_bridge_session(identity.session.session_value).policy,
                    **delivery('license', owner, identity, signers, body['release_identity']))
        package = json.loads(owner.release.package_binding)
        parts = package['version'].split('.')
        parts[-1] = str(int(parts[-1]) + 1)
        package.update(version='.'.join(parts), desktop_sha256='d'*64)
        pins = dict(json.loads(owner.release.release_pins), release_version=package['version'])
        later = replace(owner.release, desktop_sha256='d'*64, package_binding=canonical(package), release_pins=canonical(pins))
        monkeypatch.setattr(preparation, 'load_preparation_release', lambda: SimpleNamespace(execution=later))
        audit(owner)


def test_real_delivery_matched_backup_restore_is_repeatable(tmp_path, monkeypatch):
    from app.persistence.authoritative_backup import (create_authoritative_backup, verify_authoritative_backup,
        restore_authoritative_backup, OfflineAuthoritativeRestoreOwner)
    with registered(tmp_path/'live', TextAnalysisSchemas()) as (owner, identity, signers):
        monkeypatch.setattr(preparation, 'load_preparation_release', lambda: SimpleNamespace(execution=owner.release))
        signature, body = envelope(owner.release, signers['release'])
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, signature))
        policy = identity.auth.read_bridge_session(identity.session.session_value).policy
        owner.adopt(policy, **delivery('license', owner, identity, signers, body['release_identity']))
        pair = owner.pair_owner
        manifest = create_authoritative_backup(pair, tmp_path/'snapshot')
        assert verify_authoritative_backup(tmp_path/'snapshot') == manifest
        offline = OfflineAuthoritativeRestoreOwner.from_existing(root_lease=pair.root_lease,
            auth_path=pair.auth_database.path, canonical_path=pair.canonical_database.path,
            installation_id=pair.installation_id)
        first = restore_authoritative_backup(offline, tmp_path/'snapshot')
        audit(owner)
        second = restore_authoritative_backup(offline, tmp_path/'snapshot')
        audit(owner)
        assert first.incarnation != second.incarnation
        with pair.canonical_database.transaction() as db:
            db.execute('UPDATE authoritative_pair SET valid=0')
        with pytest.raises(RuntimeAuthorizationDenied):
            pair.establish()


@pytest.mark.parametrize('mutation', ['trust', 'envelope', 'delivery', 'principal', 'account', 'grants', 'key', 'orphan'])
def test_real_delivery_audit_refuses_unmatched_evidence(tmp_path, monkeypatch, mutation):
    with registered(tmp_path, TextAnalysisSchemas()) as (owner, identity, signers):
        monkeypatch.setattr(preparation, 'load_preparation_release', lambda: SimpleNamespace(execution=owner.release))
        signature, body = envelope(owner.release, signers['release'])
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, signature))
        owner.adopt(identity.auth.read_bridge_session(identity.session.session_value).policy, **delivery('license', owner, identity, signers, body['release_identity']))
        if mutation == 'trust':
            monkeypatch.setattr(preparation, 'load_preparation_release', lambda: None)
        else:
            with owner.authority.database.transaction() as db:
                if mutation == 'envelope':
                    db.execute('UPDATE text_analysis_release_identities SET envelope=?', (b'bad',))
                elif mutation == 'delivery':
                    db.execute('UPDATE text_analysis_deliveries SET response=?', (b'{}',))
                elif mutation in ('principal', 'account'):
                    db.execute('UPDATE text_analysis_deliveries SET ' + ('principal_id' if mutation == 'principal' else 'creator_account_id') + "='other'")
                elif mutation == 'grants':
                    db.execute("UPDATE verified_grant_references SET organization_id='other'")
                elif mutation == 'key':
                    db.execute("UPDATE installation_key_reference SET installation_key_jkt='different'")
                else:
                    db.execute('DELETE FROM text_analysis_deliveries')
        with pytest.raises(RuntimeAuthorizationDenied):
            audit(owner)
