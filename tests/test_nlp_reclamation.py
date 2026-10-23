"""Immutable findings ownership, bounded historical backfill and orphan cleanup."""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import shutil
from datetime import timedelta

import pytest

from app.analytics.database import ProjectionsDatabase
from app.analytics.nlp_findings_store import encoded
from app.analytics.nlp_reclamation import reclaim_orphans
from app.analytics.opaque_refs import account_ref
from app.analytics.identity import source_identity
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_nlp_processing_publication import fixture, runner

pytestmark = pytest.mark.ci_tier('integration')


def counts(database):
    with database.read() as db:
        return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0]
            for table in ('nlp_snapshots','nlp_records','nlp_snapshot_members'))


def populated(f):
    r,profile,_=runner(f)
    r.reconcile(ACCOUNT)
    assert r.process_one(ACCOUNT) and r.process_one(ACCOUNT)
    binding=r.findings.freeze(ACCOUNT,source_identity(f.source,ACCOUNT))
    return r,profile,binding


def test_orphans_recover_capacity_but_completed_jobs_and_current_input_survive(fixture):
    r,profile,binding=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    assert reclaim_orphans(db,ref)==0
    assert counts(db)==(1,2,2)
    r.findings.mode(ACCOUNT,'paused')
    assert reclaim_orphans(db,ref)==0
    assert counts(db)==(1,2,2)
    # Changing only the frozen canonical input leaves the same completed jobs
    # owning both records after the old, unbound manifest becomes obsolete.
    with db.transaction() as c:
        c.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1 WHERE account_ref=?',(ref,))
    assert reclaim_orphans(db,ref)==1
    assert counts(db)==(0,2,0)
    r.findings.select(ACCOUNT,profile)
    assert reclaim_orphans(db,ref)==2
    assert counts(db)==(0,0,0)
    # Reclaimed capacity can store the same real conformance results again.
    r.reconcile(ACCOUNT)
    assert r.process_one(ACCOUNT)
    assert counts(db)[1]==1


def test_live_and_retained_generation_bindings_keep_exact_bytes(fixture):
    r,profile,_=populated(fixture)
    assert r.publish_pending(ACCOUNT).changed
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    with db.read() as c:
        before=[tuple(row) for row in c.execute('SELECT * FROM nlp_generation_refs')]
        raw=[tuple(row) for row in c.execute('SELECT * FROM nlp_snapshots')]
    # A new selected input retires the old generation, but rollback still owns it.
    r.findings.select(ACCOUNT,profile)
    assert r.publish_pending(ACCOUNT).changed
    reclaim_orphans(db,ref)
    with db.read() as c:
        assert all(c.execute('SELECT 1 FROM nlp_generation_refs WHERE generation_id=? AND snapshot_id=?',
            (row[0],row[2])).fetchone() for row in before)
        for row in raw:
            assert bytes(c.execute('SELECT document FROM nlp_snapshots WHERE account_ref=? AND snapshot_id=?',row[:2]).fetchone()[0])==bytes(row[2])
    fixture.stores.projections.rollback_retention=0
    assert fixture.stores.projections.collect_garbage(ref)==1
    assert counts(db)[1]==0
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM nlp_generation_refs').fetchone()[0]==1


def test_shared_record_survives_one_generation_retirement(fixture):
    r,_,binding=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    pipeline=r.frozen_pipeline(ACCOUNT)
    pipeline.project_account(ACCOUNT)
    pipeline.publish_candidate(pipeline.build_candidate(ACCOUNT,force=True,full_rebuild=True))
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM nlp_generation_refs WHERE snapshot_id=?',(binding.snapshot_id,)).fetchone()[0]==2
    fixture.stores.projections.rollback_retention=0
    assert fixture.stores.projections.collect_garbage(ref)==1
    assert counts(db)==(1,2,2)
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM nlp_generation_refs WHERE snapshot_id=?',(binding.snapshot_id,)).fetchone()[0]==1


def test_record_shared_by_distinct_snapshots_survives_old_snapshot_cleanup(fixture):
    r,profile,_=runner(fixture)
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    assert r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    with db.read() as c:
        assert c.execute('SELECT count(DISTINCT snapshot_id) FROM nlp_snapshot_members').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM (SELECT record_digest FROM nlp_snapshot_members '
            'GROUP BY record_digest HAVING count(*)=2)').fetchone()[0]==1
    r.findings.select(ACCOUNT,profile)  # No job remains to disguise ownership.
    fixture.stores.projections.rollback_retention=0
    assert fixture.stores.projections.collect_garbage(ref)==1
    assert counts(db)==(1,2,2)
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM nlp_jobs').fetchone()[0]==0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('damage',['missing','wrong','manifest','guard'])
def test_corrupt_membership_never_authorizes_record_erasure(fixture,damage):
    r,profile,_=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    reclaim_orphans(db,ref)
    r.findings.select(ACCOUNT,profile)
    with db.transaction() as c:
        if damage=='guard':
            c.execute('DROP TRIGGER nlp_members_delete')
        elif damage=='manifest':
            sql=c.execute("SELECT sql FROM sqlite_master WHERE name='nlp_snapshots_immutable'").fetchone()[0]
            c.execute('DROP TRIGGER nlp_snapshots_immutable')
            c.execute("UPDATE nlp_snapshots SET document=X'7b7d'")
            c.execute(sql)
        else:
            name='nlp_members_delete' if damage=='missing' else 'nlp_members_immutable'
            sql=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0]
            c.execute('DROP TRIGGER '+name)
            if damage=='missing':
                c.execute('DELETE FROM nlp_snapshot_members WHERE ordinal=0')
            else:
                c.execute('UPDATE nlp_snapshot_members SET record_digest=(SELECT record_digest FROM nlp_records ORDER BY record_digest LIMIT 1)')
            c.execute(sql)
    with pytest.raises(ValueError,match='nlp_'):
        reclaim_orphans(db,ref)
    assert counts(db)[1]==2


def test_membership_cannot_be_changed_or_deleted_under_a_manifest(fixture):
    _,_,_=populated(fixture)
    db=fixture.stores.database
    reclaim_orphans(db,account_ref(ACCOUNT))
    for statement in ('DELETE FROM nlp_snapshot_members',
            'UPDATE nlp_snapshot_members SET ordinal=ordinal+5',
            'DELETE FROM nlp_snapshot_inventory',
            'UPDATE nlp_snapshot_inventory SET complete=0'):
        with pytest.raises(Exception,match='nlp_'):
            with db.transaction() as c:
                c.execute(statement)


def test_backfill_and_retirement_resume_after_reopen_in_bounded_pages(fixture,monkeypatch):
    import app.analytics.nlp_reclamation as reclamation
    r,profile,binding=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    assert reclamation.BATCH_SIZE==64
    monkeypatch.setattr(reclamation,'BATCH_SIZE',1)
    assert reclaim_orphans(db,ref)==0
    assert counts(db)==(1,2,1)
    r.findings.select(ACCOUNT,profile)
    reopened=ProjectionsDatabase(db.path)
    # Second page seals the manifest, then retires one membership only.
    assert reclaim_orphans(reopened,ref)==0
    assert counts(db)==(1,2,1)
    with db.read() as c:
        assert tuple(c.execute('SELECT complete,retired_ordinal FROM nlp_snapshot_inventory').fetchone())==(2,1)
    reopened=ProjectionsDatabase(db.path)
    assert reclaim_orphans(reopened,ref)==1
    assert counts(db)==(0,2,0)
    assert reclaim_orphans(reopened,ref)==1
    assert reclaim_orphans(reopened,ref)==1
    assert counts(db)==(0,0,0)


def test_record_sweep_is_bounded_and_does_not_starve_after_live_owners(fixture):
    r,_,_=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    reclaim_orphans(db,ref)
    with db.transaction() as c:
        for n in range(130):
            raw=('orphan-'+str(n)).encode()
            c.execute('INSERT INTO nlp_records VALUES(?,?,?)',(ref,hashlib.sha256(raw).hexdigest(),raw))
    removed=[]
    for _ in range(6):
        removed.append(reclaim_orphans(db,ref))
    assert sum(removed)==130 and max(removed)<=64
    assert counts(db)==(1,2,2)


@pytest.mark.parametrize('change',['selection','snapshot','completion'])
def test_writer_rechecks_changes_after_admission(fixture,monkeypatch,change):
    r,profile,binding=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    reclaim_orphans(db,ref)
    with db.read() as c:
        document=bytes(c.execute('SELECT document FROM nlp_snapshots').fetchone()[0])
    r.findings.select(ACCOUNT,profile)
    original=db.transaction
    fired=[]
    @contextmanager
    def racing_transaction(*args,**kwargs):
        if not fired:
            fired.append(True)
            with original() as c:
                if change=='snapshot':
                    value=json.loads(document)
                    value['canonical_content_digest']='sha256:'+'f'*64
                    raw=encoded(value)
                    c.execute('INSERT INTO nlp_snapshots VALUES(?,?,?)',(ref,hashlib.sha256(raw).hexdigest(),raw))
                else:
                    field='selection_epoch' if change=='selection' else 'completion_revision'
                    c.execute('UPDATE nlp_accounts SET '+field+'='+field+'+1 WHERE account_ref=?',(ref,))
        with original(*args,**kwargs) as c:
            yield c
    monkeypatch.setattr(db,'transaction',racing_transaction)
    assert reclaim_orphans(db,ref)==0
    assert counts(db)[1]==2
    assert fired


def test_catalog_upgrade_preserves_historical_bytes_and_backfills_lazily(fixture,tmp_path):
    from app.analytics.validation_receipt import content_stamp
    _,_,_=populated(fixture)
    catalog=tmp_path/'catalog26'
    catalog.mkdir()
    for path in sorted((Path(__file__).resolve().parents[1]/'app/analytics/sql').glob('*.sql'))[:26]:
        shutil.copy2(path,catalog/path.name)
    old=ProjectionsDatabase(tmp_path/'old.sqlite3',migrations_dir=catalog)
    originals={}
    with fixture.stores.database.read() as source, old.transaction() as target:
        for table in ('nlp_accounts','nlp_records','nlp_snapshots'):
            # Populate exactly the historical schema, including after later
            # metadata-only columns have been added to the current fixture.
            columns=','.join(row[1] for row in target.execute('PRAGMA table_info('+table+')'))
            rows=[tuple(row) for row in source.execute('SELECT '+columns+' FROM '+table)]
            originals[table]=(columns,rows)
            target.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        before=content_stamp(target)
    upgraded=ProjectionsDatabase(old.path)
    with upgraded.read() as c:
        assert content_stamp(c) is not None and content_stamp(c)!=before
        for table,(columns,rows) in originals.items():
            assert [tuple(row) for row in c.execute('SELECT '+columns+' FROM '+table)]==rows
        assert tuple(c.execute('SELECT previous_profile_json,dependency_intent FROM nlp_accounts').fetchone())==(None,None)
        assert c.execute('SELECT count(*) FROM nlp_snapshot_members').fetchone()[0]==0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert reclaim_orphans(upgraded,account_ref(ACCOUNT))==0
    assert counts(upgraded)==(1,2,2)


def test_retention_retirement_revokes_pending_snapshot_root(fixture):
    r,_,binding=populated(fixture)
    assert r.publish_pending(ACCOUNT).changed
    db=fixture.stores.database
    fixture.clock.now+=timedelta(days=100)
    assert fixture.stores.projections.enforce_retention(ACCOUNT)
    # Passive retention reads release bindings and persist expiry; explicit
    # generation maintenance performs the bounded orphan work.
    fixture.stores.projections.collect_garbage(account_ref(ACCOUNT))
    with db.read() as c:
        assert not c.execute('SELECT 1 FROM nlp_generation_refs WHERE snapshot_id=?',(binding.snapshot_id,)).fetchone()
        assert not c.execute('SELECT 1 FROM nlp_snapshots WHERE snapshot_id=?',(binding.snapshot_id,)).fetchone()
        # Source/job erasure is a separate lifecycle action; GC preserves jobs.
        assert c.execute("SELECT count(*) FROM nlp_jobs WHERE state='done'").fetchone()[0]==2
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_findings_backfill_preserves_bounded_baseline_enrichment_proof(fixture,monkeypatch):
    from app.analytics import conversation_enrichment_unit_sql as storage
    _,_,_=populated(fixture)
    fixture.pipeline.project_account(ACCOUNT)
    store=fixture.stores.projections
    with fixture.stores.database.read() as c:
        generation=c.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
        assert store._trusted_conversation_enrichment_proof(c,generation) is not None
    def unexpected_parse(*args,**kwargs):
        raise AssertionError('unchanged enrichment was parsed')
    monkeypatch.setattr(storage,'_validate_unit',unexpected_parse)
    store.collect_garbage(account_ref(ACCOUNT))
    with fixture.stores.database.read() as c:
        assert store._trusted_conversation_enrichment_proof(c,generation) is not None


def test_baseline_without_findings_skips_membership_audit_and_writes(fixture,monkeypatch):
    import app.analytics.nlp_reclamation as reclamation
    fixture.pipeline.project_account(ACCOUNT)
    def unexpected_audit(*args,**kwargs):
        raise AssertionError('empty findings partition was audited')
    monkeypatch.setattr(reclamation,'content_stamp',unexpected_audit)
    assert fixture.stores.projections.collect_garbage(account_ref(ACCOUNT))==0
    with fixture.stores.database.read() as c:
        assert c.execute('SELECT count(*) FROM nlp_reclamation_cursor').fetchone()[0]==0


@pytest.mark.parametrize('operation',['read','publish'])
def test_corrupt_orphan_cannot_revoke_baseline_read_or_committed_publication(fixture,monkeypatch,caplog,operation):
    import app.analytics.nlp_reclamation as reclamation
    r,profile,_=populated(fixture)
    baseline=fixture.pipeline.project_account(ACCOUNT)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    r.findings.select(ACCOUNT,profile)
    with db.transaction() as c:
        sql=c.execute("SELECT sql FROM sqlite_master WHERE name='nlp_members_delete'").fetchone()[0]
        c.execute('DROP TRIGGER nlp_members_delete')
        c.execute('DELETE FROM nlp_snapshot_members WHERE ordinal=0')
        c.execute(sql)
    before=counts(db)
    original=reclamation.reclaim_orphans
    calls=[]
    def observed(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(reclamation,'reclaim_orphans',observed)
    caplog.clear()
    if operation=='read':
        assert fixture.stores.projections.get_artifact(ACCOUNT)==baseline.artifact
        assert not calls  # Retention reads do not audit even one account.
    else:
        fixture.stores.projections.rollback_retention=0
        run=fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT,force=True,full_rebuild=True))
        assert run.changed and calls
        assert fixture.stores.projections.get_artifact(ACCOUNT)==run.artifact
        assert any(record.message=='analytics_cleanup_event status=incomplete reason_code=findings_reclamation_failed'
            for record in caplog.records)
        with db.read() as c:
            assert c.execute("SELECT count(*) FROM projection_generations WHERE status='retired'").fetchone()[0]==0
    assert counts(db)==before
    with pytest.raises(ValueError,match='nlp_membership_invalid'):
        original(db,ref)
    assert counts(db)==before


def test_expired_cleanup_budget_fails_closed_without_reclaiming_content(fixture,monkeypatch):
    import app.analytics.nlp_reclamation as reclamation
    r,profile,_=populated(fixture)
    db=fixture.stores.database
    ref=account_ref(ACCOUNT)
    reclaim_orphans(db,ref)
    r.findings.select(ACCOUNT,profile)
    before=counts(db)
    monkeypatch.setattr(reclamation,'VERIFICATION_SECONDS',0)
    with pytest.raises(ValueError,match='nlp_findings_deadline'):
        reclaim_orphans(db,ref)
    assert counts(db)==before
