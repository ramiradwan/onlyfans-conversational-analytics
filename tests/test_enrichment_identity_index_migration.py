"""Candidate-only migration: compact lookups keep content and authority checks intact."""
from pathlib import Path
import hashlib
import json
import shutil
from unittest.mock import patch
import pytest
from app.analytics.database import ProjectionsDatabase
from app.analytics.validation_receipt import content_stamp,ValidationReceipt,ValidationReceipts,generation_binding
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence import sqlite_api as sqlite3
from app.persistence.migrations import SchemaCompatibilityError
from tests.continuous_analytics_fixture import make_fixture,cleanup,ACCOUNT,NOW,cold_equal,insert_message,advance
ROOT=Path(__file__).resolve().parents[1]
INDEX='conversation_enrichment_unit_identity'


def old_catalog(directory):
    path=directory/'schema24';path.mkdir()
    for p in sorted((ROOT/'app/analytics/sql').glob('*.sql'))[:24]:shutil.copy2(p,path/p.name)
    return path

def test_upgrade_preserves_enrichment_bytes_backup_and_invalidates_receipt(tmp_path):
    from app.analytics.pipeline import AnalyticsPipeline
    f=make_fixture(tmp_path/'canonical',backend='memory',conversations=3,messages=8)
    catalog=old_catalog(tmp_path)
    old=ProjectionsDatabase(tmp_path/'projection.sqlite3',migrations_dir=catalog)
    options=dict(activation=f.repositories.projection_activation,canonical_identity_reader=f.source.read_identity)
    store=SQLiteAnalyticsProjectionStore(old,**options)
    pipeline=AnalyticsPipeline(f.source,projections=store,enrichment=f.pipeline.enrichment,clock=lambda:NOW)
    try:
        expected=pipeline.project_account(ACCOUNT).artifact
        with old.read() as db:
            before=content_stamp(db);gen=dict(db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone())
            payloads=[tuple(r) for r in db.execute('SELECT * FROM conversation_enrichment_units ORDER BY creator_account_id,unit_id')]
        upgraded=ProjectionsDatabase(old.path)
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==25
            assert [tuple(r) for r in db.execute('SELECT * FROM conversation_enrichment_units ORDER BY creator_account_id,unit_id')]==payloads
            after=content_stamp(db)
            assert before!=after and before[:2]==after[:2] and before[3]==after[3]
            assert _guards_match(db)
            indexes={r[1]:r for r in db.execute('PRAGMA index_list(conversation_enrichment_units)')}
            assert indexes[INDEX][2]==1
            assert [r[2] for r in db.execute('PRAGMA index_info('+INDEX+')')]==['creator_account_id','unit_id']
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            receipts=ValidationReceipts(monotonic=lambda:0)
            receipts.put(ValidationReceipt(gen['generation_id'],before,generation_binding(gen),100))
            db.execute('BEGIN')
            assert not receipts.take(db,gen)
            db.rollback()
        current=SQLiteAnalyticsProjectionStore(upgraded,**options)
        assert current.get_artifact(ACCOUNT)==expected
        backup=upgraded.migration_runner.last_backup_path
        assert backup is not None
        assert backup.read_bytes()[:16] != b'SQLite format 3\x00'
        with upgraded.open_detached(backup,read_only=True) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==24
            assert not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(INDEX,)).fetchone()
            assert [tuple(r) for r in db.execute('SELECT * FROM conversation_enrichment_units ORDER BY creator_account_id,unit_id')]==payloads
        with pytest.raises(SchemaCompatibilityError):ProjectionsDatabase(old.path,migrations_dir=catalog)
    finally:cleanup(f)


def test_failed_index_migration_rolls_back_completely(tmp_path):
    catalog=old_catalog(tmp_path)
    old=ProjectionsDatabase(tmp_path/'projection.sqlite3',migrations_dir=catalog)
    bad=tmp_path/'bad';shutil.copytree(catalog,bad)
    migration=ROOT/'app/analytics/sql/0025_enrichment_unit_identity_index.sql'
    (bad/migration.name).write_text(migration.read_text()+'\nINVALID REFERENCE MIGRATION;\n')
    with pytest.raises(sqlite3.DatabaseError):ProjectionsDatabase(old.path,migrations_dir=bad)
    with old.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==24
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(INDEX,)).fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert _guards_match(db) and content_stamp(db) is not None


def test_package_catalog_binds_new_index_migration():
    policy=json.loads((ROOT/'packaging/runtime-files.json').read_text())
    item=next(c for c in policy['sql_catalogs'] if c['path']=='_internal/app/analytics/sql')
    for name,digest in item['files'].items():
        assert hashlib.sha256((ROOT/'app/analytics/sql'/name).read_bytes()).hexdigest()==digest
    assert '0025_enrichment_unit_identity_index.sql' in item['files']


def test_missing_index_does_not_grant_trust_or_break_correctness(tmp_path):
    f=make_fixture(tmp_path,conversations=3,messages=8)
    try:
        expected=f.pipeline.project_account(ACCOUNT).artifact
        with f.stores.database.transaction() as db:
            before=content_stamp(db)
            db.execute('DROP INDEX '+INDEX)
            after=content_stamp(db)
            assert before!=after and before[:2]==after[:2]
            assert _guards_match(db)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','without-index',NOW,99);advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        cold_equal(f,result.artifact)
    finally:cleanup(f)



def test_new_index_matches_experiment_and_existing_primary_key(tmp_path):
    from tools.analytics_enrichment_reference_sql import INDEX_SQL
    database=ProjectionsDatabase(tmp_path/'analytics.sqlite3')
    with database.read() as db:
        actual=db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(INDEX,)).fetchone()[0]
        assert ' '.join(actual.split()).rstrip(';')==INDEX_SQL
        columns=list(db.execute('PRAGMA table_info(conversation_enrichment_units)'))
        assert [r['name'] for r in sorted(columns,key=lambda r:r['pk']) if r['pk']]==['creator_account_id','unit_id']
        assert all(r['notnull'] for r in columns if r['pk'])
        assert 'WITHOUT ROWID' in db.execute("SELECT sql FROM sqlite_master WHERE name='conversation_enrichment_units'").fetchone()[0]


def test_normal_migration_uses_compact_index_and_preserves_real_transition(tmp_path):
    from tools.analytics_enrichment_reference_sql import EnrichmentReferenceFixture
    from tools.analytics_insertion_diagnostic import Attribution
    fixture=EnrichmentReferenceFixture(tmp_path,1000,NOW)
    try:
        assert fixture.configuration['user_version']==25
        assert fixture.index_creation_seconds is None  # No diagnostic DDL injection.
        plans=fixture.plans()
        reads=[r['object'] for r in plans['copy']['opened_objects'] if r['opcode']=='OpenRead']
        assert INDEX in reads and 'conversation_enrichment_units' not in reads
        assert any('COVERING INDEX '+INDEX in str(row) for row in plans['references']['eqp'])
        sample=fixture.sample(0,Attribution(enabled=False))
        for check in ['independent_source_and_persisted_equal','foreign_keys_valid','shared_units_retained',
                      'unshared_unit_removed','retirement_scope_closed']:
            assert sample[check] is True
        assert sample['complete_reference_comparisons']==4 and sample['commits']==3
    finally:fixture.close()


def test_fk_and_immutability_stay_enforced(tmp_path):
    from tools.analytics_enrichment_reference_sql import EnrichmentReferenceFixture
    from tools.analytics_graph_component import insert_generation
    fixture=EnrichmentReferenceFixture(tmp_path,1000,NOW)
    try:
        with fixture.database.read() as db:
            row=db.execute('SELECT * FROM conversation_enrichment_refs WHERE generation_id=? LIMIT 1',(fixture.old,)).fetchone()
            keys=list(row.keys());values=list(row);values[keys.index('generation_id')]=fixture.new;values[keys.index('unit_id')]='f'*64
            db.execute('BEGIN');insert_generation(db,fixture.new,fixture.account)
            with pytest.raises(sqlite3.IntegrityError):
                db.execute('INSERT INTO conversation_enrichment_refs ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',values)
            with pytest.raises(sqlite3.IntegrityError):
                db.execute('DELETE FROM conversation_enrichment_units WHERE creator_account_id=? AND unit_id=?',(fixture.account,row['unit_id']))
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE conversation_enrichment_units SET metrics_json='{}' WHERE creator_account_id=? AND unit_id=?",(fixture.account,row['unit_id']))
            with pytest.raises(sqlite3.IntegrityError):
                db.execute('DELETE FROM conversation_enrichment_refs WHERE generation_id=?',(fixture.old,))
            db.rollback()
    finally:fixture.close()


def test_missing_tracking_still_rejects_all_receipts(tmp_path):
    database=ProjectionsDatabase(tmp_path/'analytics.sqlite3')
    with database.read() as db:
        assert _guards_match(db) and content_stamp(db) is not None
        for name, in list(db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'generation_content_%'")):
            db.execute('BEGIN')
            db.execute('DROP TRIGGER "'+name+'"')
            assert not _guards_match(db) and content_stamp(db) is None
            db.rollback()


def test_missing_parent_remains_visible_and_cannot_gain_proof(tmp_path):
    from tools.analytics_enrichment_reference_sql import EnrichmentReferenceFixture
    from app.analytics.enrichment_proof_transition import _references
    fixture=EnrichmentReferenceFixture(tmp_path,1000,NOW)
    try:
        with fixture.database.read() as db:
            # Corrupt a disposable database; production settings remain unchanged.
            db.execute('PRAGMA foreign_keys=OFF');db.execute('BEGIN')
            db.execute('DROP TRIGGER conversation_enrichment_units_referenced')
            db.execute('DELETE FROM conversation_enrichment_units WHERE creator_account_id=? AND unit_id=?',(fixture.account,fixture.previous.header.unit_id))
            rows=_references(db,fixture.old,101)
            assert len(rows)==101 and any(r[8] is None for r in rows)
            assert content_stamp(db) is None and not _guards_match(db)
            db.rollback();db.execute('PRAGMA foreign_keys=ON')
    finally:fixture.close()
