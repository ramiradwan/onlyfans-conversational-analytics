"""SQL reference lifecycle diagnostic; actual schema, no graph-authority claim."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import ast
import hashlib
import json
import time
import zlib


def graph_link_query():
    from app.analytics import sqlite_projection_store
    source=Path(sqlite_projection_store.__file__).read_text(encoding='utf-8')
    function=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_validate_generation_links')
    return next(n.value for n in ast.walk(function) if isinstance(n,ast.Constant)
                and isinstance(n.value,str) and 'LEFT JOIN conversation_graph_units' in n.value)


def unit_for(account,conversation,participant,count,index,clock):
    from app.analytics.conversation_graph_units import ConversationGraphUnit,ConversationGraphUnitHeader,unit_id
    nodes=['g1:'+hashlib.sha256(f'sql-node-{index}-{i}'.encode()).hexdigest() for i in range(3*count)]
    edges=['e1:'+hashlib.sha256(f'sql-edge-{index}-{i}'.encode()).hexdigest() for i in range(7*count)]
    nodes.sort();edges.sort()
    a=json.dumps(nodes,separators=(',',':')).encode();b=json.dumps(edges,separators=(',',':')).encode()
    digest='sha256:'+hashlib.sha256(a+b).hexdigest()
    header=ConversationGraphUnitHeader(account,conversation,digest,'sha256:'+'b'*64,
        clock-timedelta(days=90),clock+timedelta(days=30),participant,clock-timedelta(days=2),clock,
        digest,len(nodes),len(edges),unit_id(digest,nodes,edges))
    return ConversationGraphUnit(header,zlib.compress(a,6),zlib.compress(b,6))


def payload_hash(unit):
    from app.analytics.conversation_graph_units import graph_unit_ids,unit_id
    # Independently decode both frames and recalculate their identity each time.
    nodes=tuple(json.loads(zlib.decompress(unit.node_ids)))
    edges=tuple(json.loads(zlib.decompress(unit.edge_ids)))
    if (len(nodes)!=unit.header.node_count or len(edges)!=unit.header.edge_count
            or unit_id(unit.header.graph_digest,nodes,edges)!=unit.header.unit_id
            or graph_unit_ids(unit)!=(nodes,edges)):
        raise ValueError('reference_component_membership_mismatch')
    return hashlib.sha256(unit.node_ids+unit.edge_ids).hexdigest()


class ReferenceSQLFixture:
    def __init__(self,directory,total,clock):
        from app.analytics.database import ProjectionsDatabase
        from app.analytics.opaque_refs import account_ref,conversation_ref,participant_ref
        from app.analytics import conversation_graph_unit_sql as sql
        from tools.analytics_graph_component import insert_generation
        self.total,self.clock=total,clock
        self.database=ProjectionsDatabase(directory/'reference.sqlite3')
        self.db=self.database.connect();db=self.db
        self.account=account_ref('synthetic-reference-owner')
        self.old='00000000-0000-0000-0000-000000000051'
        self.new='00000000-0000-0000-0000-000000000052'
        self.check_query=graph_link_query()
        participant=participant_ref('synthetic-reference-owner','participant')
        db.execute('BEGIN')
        db.execute("INSERT INTO projection_publication_epochs VALUES ('component-epoch','component-owner',?,'open','2026-10-01T00:00:00.000000Z',NULL)",('sha256:'+'a'*64,))
        insert_generation(db,self.old,self.account)
        self.shape=[]
        for chat in range(101):
            count=total//2+1 if chat==0 else max(1,total//200)
            ref=conversation_ref('synthetic-reference-owner',str(chat))
            unit=unit_for(self.account,ref,participant,count,chat,clock)
            if sql.insert_units(db,self.old,[unit])!=1:
                raise ValueError('reference_fixture_unit_missing')
            payload_hash(unit)
            self.shape.append(dict(conversation=chat,messages=count,node_bytes=len(unit.node_ids),edge_bytes=len(unit.edge_ids)))
            if chat==0:self.previous=unit
        self.changed=unit_for(self.account,self.previous.header.conversation_ref,participant,total//2+2,101,clock)
        payload_hash(self.changed)
        for status in ('validated','activation_pending','active'):
            if status=='activation_pending':
                db.execute("UPDATE projection_generations SET status=?,activation_intent_id='component-witness',witness_sequence=1 WHERE generation_id=?",(status,self.old))
            elif status=='active':
                db.execute('UPDATE projection_generations SET status=?,activated_at=? WHERE generation_id=?',(status,clock.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),self.old))
            else:db.execute('UPDATE projection_generations SET status=? WHERE generation_id=?',(status,self.old))
        insert_generation(db,self.new,self.account,self.old);db.commit()
        self.references=tuple(r for r in sql.list_references(db,self.old,self.account)
                              if r.header.conversation_ref!=self.previous.header.conversation_ref)
        self.metadata=dict(account_messages_basis=total,conversations=101,unchanged_references=len(self.references),
            shape=self.shape,previous_unit_id=self.previous.header.unit_id,new_unit_id=self.changed.header.unit_id,
            data_scope='Synthetic valid membership frames with deterministic SQL contents. No canonical/graph-payload equivalence or publication authority is claimed.')

    def plans(self):
        from app.analytics import sqlite_projection_store as store
        db=self.db
        db.execute('CREATE TEMP TABLE IF NOT EXISTS retirement_graph_unit_ids(unit_id TEXT PRIMARY KEY) WITHOUT ROWID')
        reclaim=store._SCOPED_GRAPH_RETIREMENT_RECLAIM
        arguments=(self.account,)*reclaim.count('?')
        names={row[1]:row[0] for row in db.execute("SELECT name,rootpage FROM sqlite_master WHERE type IN ('table','index')")}
        from app.analytics import conversation_graph_unit_sql as sql
        tree=ast.parse(Path(sql.__file__).read_text(encoding='utf-8'))
        copy=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)
                  and 'INSERT INTO conversation_graph_refs' in n.value and 'SELECT ?' in n.value)
        h=self.references[0].header
        values=(self.new,self.old,self.account,h.conversation_ref,h.input_digest,h.config_digest,h.unit_id)
        result={}
        for name,q,a in [('copy',copy,values),('closure',self.check_query,(self.old,self.account)),('reclaim',reclaim,arguments)]:
            result[name]=dict(eqp=[list(r) for r in db.execute('EXPLAIN QUERY PLAN '+q,a)],
                opened_objects=[dict(opcode=r[1],object=names.get(r[3]),database=r[4])
                    for r in db.execute('EXPLAIN '+q,a) if r[1] in ('OpenRead','OpenWrite')])
        return result

    def verify(self):
        from app.analytics import conversation_graph_unit_sql as sql
        from app.analytics.opaque_refs import conversation_ref,participant_ref
        db=self.db
        participant=participant_ref('synthetic-reference-owner','participant')
        # Freshly regenerate the declared membership inputs for every sample.
        # Never use the preceding sample's expected result or PASS.
        expected={}
        for chat in range(101):
            count=self.total//2+2 if chat==0 else max(1,self.total//200)
            ref=conversation_ref('synthetic-reference-owner',str(chat))
            unit=unit_for(self.account,ref,participant,count,101 if chat==0 else chat,self.clock)
            expected[ref]=(unit.header,payload_hash(unit))
        refs=sql.list_references(db,self.new,self.account)
        if len(refs)!=len(expected):raise ValueError('reference_count_differs')
        values=[]
        for ref in refs:
            stored=sql.load_unit(db,self.new,self.account,ref.header.conversation_ref)
            actual=None if stored is None else payload_hash(stored)
            if stored is None or (stored.header,actual)!=expected[ref.header.conversation_ref]:
                raise ValueError('reference_content_differs')
            values.append((stored.header.conversation_ref,stored.header.unit_id,actual))
        if db.execute('SELECT 1 FROM conversation_graph_refs WHERE generation_id=?',(self.old,)).fetchone():
            raise ValueError('retired_references_remain')
        if db.execute('SELECT 1 FROM conversation_graph_units WHERE creator_account_id=? AND unit_id=?',(self.account,self.previous.header.unit_id)).fetchone():
            raise ValueError('unshared_unit_not_reclaimed')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('foreign_key_failure')
        if tuple(db.execute('SELECT page_retirement,graph_retirement FROM generation_content_bulk_cleanup').fetchone())!=(0,0):
            raise ValueError('retirement_scope_left_armed')
        return hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()

    def sample(self,index,trace):
        from app.analytics import conversation_graph_unit_sql as sql
        from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
        from app.analytics.database import generation_verification_cache,generation_retirement_cache
        db=self.db;db.execute('BEGIN');db.execute('SAVEPOINT sample');times={};trace.phase='reference-sample-'+str(index)
        def measured(name,fn):
            start=time.monotonic();cpu=time.thread_time();before=db.total_changes
            with trace.span('reference.'+name):value=fn()
            times[name]=dict(seconds=time.monotonic()-start,thread_cpu_seconds=time.thread_time()-cpu,changes=db.total_changes-before)
            return value
        try:
            start=time.monotonic()
            # Match the production cache scopes. Both storing units and
            # persisted validation use 32 MiB; synchronous retirement uses its
            # existing 128 MiB target. These are unchanged runtime policies.
            with generation_verification_cache(db):
                if measured('store_changed_unit',lambda:sql.insert_units(db,self.new,[self.changed]))!=1:
                    raise ValueError('changed_unit_not_inserted')
                if measured('copy_unchanged_references',lambda:sql.insert_units(db,self.new,self.references))!=100:
                    raise ValueError('reference_copy_incomplete')
                if measured('validate_graph_links',lambda:db.execute(self.check_query,(self.new,self.account)).fetchone()) is not None:
                    raise ValueError('reference_closure_failed')
            with generation_retirement_cache(db):
                measured('retire_predecessor',lambda:SQLiteAnalyticsProjectionStore._retire_active_generation(
                    db,self.old,self.account,'2026-10-01T00:00:00.000000Z'))
            seconds=time.monotonic()-start
            at=time.monotonic();digest=self.verify();oracle=time.monotonic()-at
            return dict(index=index,intervals=times,transition_seconds=seconds,oracle_seconds=oracle,
                independent_membership_and_content_equal=True,foreign_keys_valid=True,retirement_scope_closed=True,
                unshared_unit_removed=True,shared_units_retained=True,output_digest=digest)
        finally:
            db.execute('ROLLBACK TO sample');db.execute('RELEASE sample');db.rollback()

    def close(self):
        self.db.close();self.database.release_wal_anchor()


async def run_reference_sql_component(args,q,light,outer,status,manifest,result,workdir):
    from tools.analytics_insertion_diagnostic import Attribution
    from app.analytics.database import GENERATION_VERIFICATION_CACHE_KIB, GENERATION_RETIREMENT_CACHE_KIB
    workdir.mkdir(parents=True);begun=time.monotonic()
    light.atomic_status(status,'reference-sql-preparation')
    fixture=ReferenceSQLFixture(workdir,args.messages,datetime.fromisoformat(manifest['fixture']['evaluation_clock']))
    result.update(fixture=fixture.metadata,preparation_seconds=time.monotonic()-begun,samples=[],
        schema='a07-reference-sql-component.v2',query_plans=fixture.plans(),
        cache_scopes=dict(store_and_validation_kib=GENERATION_VERIFICATION_CACHE_KIB,retirement_kib=GENERATION_RETIREMENT_CACHE_KIB),
        scope='Actual schema, storage, reference-copy, closure-check and synchronous retirement; independent byte/key oracle. Not scheduler visibility or graph-content qualification.')
    trace=Attribution(enabled=args.trace_mode!='none')
    try:
        for index in range(args.focused_repeats):
            light.atomic_status(status,'reference-sql-sample',index=index)
            value=fixture.sample(index,trace);result['samples'].append(value)
            q.write_once(args.output/'samples'/f'{index:02}.json',value)
        result['complete']=True
    finally:
        trace.restore();fixture.close()
        result['attribution']=trace.events
        result['safety']=dict(component_database_closed=True,scheduler_used=False,publication_authority_tested=False)
    result['summary']=dict(samples=len(result['samples']),references=101)
