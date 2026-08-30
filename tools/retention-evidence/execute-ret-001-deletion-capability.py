#!/usr/bin/env python3
"""Isolated RET-001 selective/age deletion capability observations.

These operations are characterization-only direct SQL against synthetic temporary
stores. They prove technical capability; they are not Product deletion APIs or
retention policy implementation.
"""
from __future__ import annotations
import argparse, json, os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
os.environ.setdefault('ENVIRONMENT','test')
os.environ.setdefault('OFCA_TEST_DATABASE_MASTER_KEY_HEX','4d7d278b49d90e4d747ac36d4f65661ec089df85a8f6f22851a628c880f7a4e2')
from app.persistence.factory import create_canonical_repositories
from ret001_execution import require_product_revision, write_json
import importlib.util, sys
LIB=Path(__file__).with_name('run-ret-001-companion-observations.py')
spec=importlib.util.spec_from_file_location('ret001_observation_lib',LIB);lib=importlib.util.module_from_spec(spec);sys.modules[spec.name]=lib;spec.loader.exec_module(lib)
def counts(db,account):
    with db.read() as c:
        return {k:int(c.execute(q,(account,)).fetchone()[0]) for k,q in {
          'messages':'SELECT COUNT(*) FROM account_messages WHERE creator_account_id=? AND is_deleted=0',
          'chats':'SELECT COUNT(*) FROM account_chats WHERE creator_account_id=? AND is_deleted=0',
          'raw_events':'SELECT COUNT(*) FROM raw_ingest_events WHERE creator_account_id=?'}.items()}
def run(root,revision):
    repos=create_canonical_repositories('sqlite',canonical_path=root/'canonical.sqlite3',projection_path=root/'projections.sqlite3')
    account,_=lib.seed_canonical_snapshot(repos.history); before=counts(repos.database,account)
    with repos.database.transaction() as c:
        c.execute("DELETE FROM account_messages WHERE creator_account_id=? AND message_id='beta-message-1'",(account,))
    selective=counts(repos.database,account)
    assert selective['messages']==before['messages']-1 and selective['chats']==before['chats']
    with repos.database.transaction() as c:
        c.execute("DELETE FROM account_messages WHERE creator_account_id=? AND sent_at < ?",(account,'2026-07-14T00:00:00Z'))
    aged=counts(repos.database,account)
    assert aged['messages']==1
    return {'schema':'ofca-ret-001-deletion-capability-observation/v1','product_revision':revision,'observed_at':datetime.now(timezone.utc).isoformat(),'production_deletion_api_added':False,'production_retention_rule_added':False,'synthetic_fixture':True,'test_only_direct_sql_deletion':True,'facts':{'before':before,'after_selective_message_delete':selective,'after_synthetic_age_cutoff':aged,'selective_message_delete_technically_feasible_in_canonical_sql':True,'age_predicate_delete_technically_feasible_in_canonical_sql':True,'cutoff':'2026-07-14T00:00:00Z','cross_store_propagation_implemented':False,'backup_rewrite_implemented':False,'stale_extension_replay_suppression_implemented':False},'interpretation':['The SQL store can select one message and can select messages by timestamp.','This does not establish safe end-to-end deletion: projections, Extension state, raw ingest/replay, backups and restore remain separate boundaries.','The cutoff is synthetic test data, not a proposed retention duration.']}
def main():
    p=argparse.ArgumentParser();p.add_argument('--product-revision',required=True);p.add_argument('--output-dir',required=True,type=Path);a=p.parse_args();require_product_revision(a.product_revision);a.output_dir.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='ret-001-delete-capability-') as t:d=run(Path(t),a.product_revision)
    dest=a.output_dir/'phase-a-deletion-capability.json';write_json(dest,d);print(json.dumps({'product_revision':a.product_revision,'output':str(dest)},indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
