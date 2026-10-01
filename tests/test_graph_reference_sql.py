"""Compact keys change access paths, never reference or publication authority."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import shutil

import pytest
from app.analytics.database import ProjectionsDatabase
from app.analytics.validation_receipt import content_stamp,ValidationReceipt,ValidationReceipts,generation_binding
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore,_validate_generation_links,_SCOPED_GRAPH_RETIREMENT_RECLAIM
from app.analytics.sqlite_graph_store import GraphReferentialIntegrityError
from app.persistence import sqlite_api as sqlite3
from app.persistence.migrations import SchemaCompatibilityError
from tests.continuous_analytics_fixture import make_fixture,cleanup,ACCOUNT,NOW,cold_equal,insert_message,advance

ROOT=Path(__file__).resolve().parents[1]
INDEX='conversation_graph_unit_identity'


def old_catalog(directory):
    path=directory/'schema23';path.mkdir()
    for p in sorted((ROOT/'app/analytics/sql').glob('*.sql'))[:23]:shutil.copy2(p,path/p.name)
    return path


def test_upgrade_preserves_graph_backup_and_invalidates_old_receipt(tmp_path):
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
            payloads=[tuple(r) for r in db.execute('SELECT * FROM conversation_graph_units ORDER BY creator_account_id,unit_id')]
        upgraded=ProjectionsDatabase(old.path)
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==25
            assert [tuple(r) for r in db.execute('SELECT * FROM conversation_graph_units ORDER BY creator_account_id,unit_id')]==payloads
            after=content_stamp(db)
            assert before!=after and before[:2]==after[:2] and before[3]==after[3]
            assert _guards_match(db)
            indexes={r[1]:r for r in db.execute('PRAGMA index_list(conversation_graph_units)')}
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
        with upgraded.open_detached(backup,read_only=True) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==23
            assert not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(INDEX,)).fetchone()
            assert [tuple(r) for r in db.execute('SELECT * FROM conversation_graph_units ORDER BY creator_account_id,unit_id')]==payloads
        with pytest.raises(SchemaCompatibilityError):ProjectionsDatabase(old.path,migrations_dir=catalog)
    finally:cleanup(f)


def test_failed_index_migration_rolls_back_completely(tmp_path):
    catalog=old_catalog(tmp_path)
    old=ProjectionsDatabase(tmp_path/'projection.sqlite3',migrations_dir=catalog)
    bad=tmp_path/'bad';shutil.copytree(catalog,bad)
    migration=ROOT/'app/analytics/sql/0024_graph_unit_identity_index.sql'
    (bad/migration.name).write_text(migration.read_text()+'\nINVALID REFERENCE MIGRATION;\n')
    with pytest.raises(sqlite3.DatabaseError):ProjectionsDatabase(old.path,migrations_dir=bad)
    with old.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==23
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(INDEX,)).fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert _guards_match(db) and content_stamp(db) is not None


def test_package_catalog_binds_new_index_migration():
    policy=json.loads((ROOT/'packaging/runtime-files.json').read_text())
    item=next(c for c in policy['sql_catalogs'] if c['path']=='_internal/app/analytics/sql')
    for name,digest in item['files'].items():
        assert hashlib.sha256((ROOT/'app/analytics/sql'/name).read_bytes()).hexdigest()==digest
    assert '0024_graph_unit_identity_index.sql' in item['files']


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


def test_foreign_key_and_immutable_guards_remain_with_index(tmp_path):
    from tools.analytics_reference_sql_component import ReferenceSQLFixture
    f=ReferenceSQLFixture(tmp_path,1000,NOW)
    db=f.db
    try:
        row=db.execute('SELECT * FROM conversation_graph_refs WHERE generation_id=? LIMIT 1',(f.old,)).fetchone()
        fields=','.join(row.keys());parameters=[row[k] for k in row.keys()];parameters[0]=f.new;parameters[-1]='f'*64
        db.execute('BEGIN')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO conversation_graph_refs ('+fields+') VALUES ('+','.join('?' for _ in parameters)+')',parameters)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM conversation_graph_units WHERE creator_account_id=? AND unit_id=?',(f.account,row['unit_id']))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE conversation_graph_units SET graph_digest=? WHERE creator_account_id=? AND unit_id=?",('sha256:'+'d'*64,f.account,row['unit_id']))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM conversation_graph_refs WHERE generation_id=?',(f.old,))
        db.rollback()
    finally:f.close()


def test_reference_first_reclaim_keeps_shared_and_unlisted_content(tmp_path):
    from tools.analytics_reference_sql_component import ReferenceSQLFixture
    from app.analytics.conversation_graph_unit_sql import insert_units
    f=ReferenceSQLFixture(tmp_path,1000,NOW);db=f.db
    try:
        db.execute('BEGIN')
        assert insert_units(db,f.new,f.references)==100
        assert insert_units(db,f.new,[f.changed])==1
        db.execute('CREATE TEMP TABLE retirement_graph_unit_ids(unit_id TEXT PRIMARY KEY) WITHOUT ROWID')
        # Every listed old unit is still referenced, so none may be removed.
        db.execute('INSERT INTO retirement_graph_unit_ids SELECT unit_id FROM conversation_graph_units WHERE creator_account_id=?',(f.account,))
        before=[tuple(r) for r in db.execute('SELECT creator_account_id,unit_id FROM conversation_graph_units')]
        assert db.execute(_SCOPED_GRAPH_RETIREMENT_RECLAIM,(f.account,f.account)).rowcount==0
        assert [tuple(r) for r in db.execute('SELECT creator_account_id,unit_id FROM conversation_graph_units')]==before
        SQLiteAnalyticsProjectionStore._retire_active_generation(db,f.old,f.account,'2026-10-01T00:00:00.000000Z')
        assert f.verify()
        db.rollback()
    finally:f.close()


def test_closure_check_still_rejects_corrupt_missing_unit(tmp_path):
    from tools.analytics_reference_sql_component import ReferenceSQLFixture
    f=ReferenceSQLFixture(tmp_path,1000,NOW);db=f.db
    try:
        db.execute('PRAGMA foreign_keys=OFF');db.execute('BEGIN')
        db.execute('DROP TRIGGER conversation_graph_units_referenced')
        db.execute('DELETE FROM conversation_graph_units WHERE creator_account_id=? AND unit_id=?',(f.account,f.previous.header.unit_id))
        with pytest.raises(GraphReferentialIntegrityError,match='conversation_graph_reference_absent'):
            _validate_generation_links(db,f.old,f.account,lambda:None)
        db.rollback();db.execute('PRAGMA foreign_keys=ON')
    finally:f.close()


def test_sql_paths_use_compact_parent_key_not_payload_for_references(tmp_path):
    from tools.analytics_reference_sql_component import ReferenceSQLFixture
    f=ReferenceSQLFixture(tmp_path,1000,NOW)
    try:
        plans=f.plans()
        reads=[r['object'] for r in plans['copy']['opened_objects'] if r['opcode']=='OpenRead']
        assert INDEX in reads and 'conversation_graph_units' not in reads
        assert any(INDEX in str(row) for row in plans['closure']['eqp'])
        # Reclamation filters the temp key set with the existing reference index.
        assert any('conversation_graph_refs_by_unit' in str(row) for row in plans['reclaim']['eqp'])
    finally:f.close()
