"""Held ownership and explicit durable authoritative pairing."""
from pathlib import Path

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.authoritative_pair import (
    AuthoritativePairOwner, FixtureAuthoritativeRootLease, RuntimeRootLease,
    RESTORE_JOURNAL_FILENAME, RUNTIME_LOCK_FILENAME,
)
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner, InstallationMigrationLock
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.test_text_analysis_execution_authority import basis

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def make_pair(root, *, establish=True, installation_id='installation'):
    root.mkdir(parents=True,exist_ok=True)
    authentication=SQLiteAuthenticationStore(root/'auth.sqlite3')
    canonical=CanonicalSQLite(root/'canonical.sqlite3')
    MigrationRunner(canonical,migrations_dir=Path(__file__).resolve().parents[1]/'app/persistence/sql',
        lock_path=root/'canonical-migration.lock',backups_dir=root/'migration-backups').run()
    owner=AuthoritativePairOwner(authentication,canonical,installation_id=installation_id,
        root_lease=FixtureAuthoritativeRootLease(root))
    if establish:
        owner.establish()
    return owner


def test_pair_requires_explicit_establishment_and_exact_retry(tmp_path):
    owner=make_pair(tmp_path,establish=False)
    with owner.auth_database.transaction() as a, owner.canonical_database.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)
    pair=owner.establish()
    assert owner.establish()==pair
    with owner.auth_database.transaction() as a, owner.canonical_database.transaction() as c:
        assert owner.validate_in(a,c)==pair
    with owner.auth_database.read() as a, owner.canonical_database.read() as c:
        a.execute('BEGIN'); c.execute('BEGIN')
        assert owner.validate_read_in(a,c)==pair


def test_foreign_held_connections_and_reconstructed_owner_are_refused(tmp_path):
    owner=make_pair(tmp_path/'a'); other=make_pair(tmp_path/'b')
    with owner.auth_database.transaction() as a, other.canonical_database.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)
    facade=CanonicalSQLite(owner.canonical_database.path)
    with owner.auth_database.transaction() as a, facade.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)
    with other.auth_database.transaction() as a, owner.canonical_database.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)


@pytest.mark.parametrize('column,value',[('incarnation','f'*32),('canonical_store_id','a'*32),('installation_id','other'),('valid',0)])
def test_mirror_mismatch_refuses_without_repair(tmp_path,column,value):
    owner=make_pair(tmp_path)
    with owner.canonical_database.transaction() as c:
        c.execute('UPDATE authoritative_pair SET '+column+'=?',(value,))
    with owner.auth_database.transaction() as a, owner.canonical_database.transaction() as c:
        before=tuple(a.execute('SELECT * FROM authoritative_pair').fetchone())
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)
        assert tuple(a.execute('SELECT * FROM authoritative_pair').fetchone())==before
    with pytest.raises(RuntimeAuthorizationDenied):
        owner.establish()


def test_any_restore_journal_refuses_live_pair_without_parsing(tmp_path):
    owner=make_pair(tmp_path)
    (tmp_path/RESTORE_JOURNAL_FILENAME).write_bytes(b'incomplete')
    with owner.auth_database.transaction() as a, owner.canonical_database.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)


def test_native_root_proof_tracks_actual_lock_lifetime(tmp_path):
    with InstallationMigrationLock(tmp_path/RUNTIME_LOCK_FILENAME) as lock:
        proof=RuntimeRootLease.from_held_lock(tmp_path,lock)
        proof.assert_current(tmp_path)
        (tmp_path/RESTORE_JOURNAL_FILENAME).write_bytes(b'incomplete')
        with pytest.raises(RuntimeAuthorizationDenied):
            proof.assert_current(tmp_path)
        proof.assert_current(tmp_path,allow_restore=True)
    with pytest.raises(RuntimeAuthorizationDenied):
        proof.assert_current(tmp_path,allow_restore=True)


def test_unheld_or_wrong_root_lock_cannot_supply_runtime_proof(tmp_path):
    with pytest.raises(RuntimeAuthorizationDenied):
        RuntimeRootLease.from_held_lock(tmp_path,InstallationMigrationLock(tmp_path/RUNTIME_LOCK_FILENAME))
    with InstallationMigrationLock(tmp_path/'different.lock') as lock:
        with pytest.raises(RuntimeAuthorizationDenied):
            RuntimeRootLease.from_held_lock(tmp_path,lock)


def test_nontransaction_and_closed_connection_are_refused(tmp_path):
    owner=make_pair(tmp_path)
    with owner.auth_database.read() as a, owner.canonical_database.read() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_read_in(a,c)
    with pytest.raises(RuntimeAuthorizationDenied):
        owner.validate_read_in(a,c)


def test_actual_live_module_fence_checks_mirror_on_held_canonical_connection(tmp_path,basis):
    from app.persistence.text_analysis_authority import FixtureTextAnalysisLifecycle
    from app.security.text_analysis_policy import require_text_analysis_fence
    auth,authority,_,_,context,policy,evidence,_=basis
    canonical=CanonicalSQLite(tmp_path/'canonical.sqlite3')
    MigrationRunner(canonical,migrations_dir=Path(__file__).resolve().parents[1]/'app/persistence/sql',
        lock_path=tmp_path/'canonical-migration.lock',backups_dir=tmp_path/'migration-backups').run()
    owner=AuthoritativePairOwner(auth,canonical,installation_id=context.installation_id,
        root_lease=FixtureAuthoritativeRootLease(tmp_path))
    owner.establish()
    lifecycle=FixtureTextAnalysisLifecycle(auth.database,canonical,pair_owner=owner)
    with authority.transition_fence(policy,context,kind='license',phase='start',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        with canonical.transaction() as c:
            assert require_text_analysis_fence(held,context,kind='license',phase='start',
                canonical_database=canonical,canonical_connection=c).reference_id==context.reference_id
        with canonical.transaction() as c:
            c.execute('UPDATE authoritative_pair SET valid=0')
        with canonical.transaction() as c:
            with pytest.raises(RuntimeAuthorizationDenied):
                require_text_analysis_fence(held,context,kind='license',phase='start',
                    canonical_database=canonical,canonical_connection=c)
