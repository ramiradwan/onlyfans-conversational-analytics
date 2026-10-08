"""Enrichment case of the existing reference-SQL diagnostic, not qualification."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import ast
import hashlib
import json
import shutil
import time

INDEX = 'conversation_enrichment_unit_identity'
INDEX_SQL = ('CREATE UNIQUE INDEX '+INDEX+
             ' ON conversation_enrichment_units(creator_account_id,unit_id)')
ACCOUNT = 'synthetic-continuous-owner'


def raw_for(total, chat, clock, operation=None):
    from tools.analytics_insertion_diagnostic import raw_fixture, mutate_raw
    raw = raw_fixture(total, clock) if chat==0 else dict(
        conversation_id=f'chat-{chat}',platform_user_id='synthetic-fan',display_name=None,
        unread_count=0,last_message_at=None,messages=[])
    if chat:
        rows = []
        for i in range(total//2+chat-1, total, 100):
            rows.append(dict(message_id=f'matrix-input-{i}',source_ordinal=len(rows),
                text='Thanks pricing',sent_at=(clock-timedelta(hours=48)+timedelta(seconds=i*48*3600/total)).isoformat(),
                direction='inbound' if i%2==0 else 'outbound',sentiment=None))
        if not rows:
            raise ValueError('enrichment_reference_fixture_too_small')
        raw = dict(raw,conversation_id=f'chat-{chat}',messages=rows,last_message_at=rows[-1]['sent_at'])
    if operation is not None:
        if chat==0:
            raw = mutate_raw(raw,operation)
        else:
            rows=[dict(r) for r in raw['messages']]
            rows.append(dict(rows[-1],message_id='visibility-new-small',source_ordinal=len(rows),
                             sent_at=(clock-timedelta(microseconds=1)).isoformat()))
            raw=dict(raw,messages=rows,last_message_at=rows[-1]['sent_at'])
    return raw


def unit_from_source(raw, clock, *, retain_analyzers=True):
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.enrichment_cache import EnrichmentReuse, ACTIVE_REUSE
    from app.analytics.conversation_enrichment_units import create_enrichment_unit
    from app.analytics.source_snapshot import conversation_digest
    from app.analytics.metrics import build_conversation_metrics
    from app.analytics.opaque_refs import conversation_ref
    from app.models.analytics import CanonicalConversation
    stage=EnrichmentStage();conversation=CanonicalConversation.model_validate(raw)
    # Empty input cache is the declared pure fixture, not a fake successful proof.
    reuse=EnrichmentReuse(SimpleNamespace(load_enrichment_entries=lambda *a,**k:{}),
                          ACCOUNT,lambda:clock)
    token=ACTIVE_REUSE.set(reuse if retain_analyzers else None)
    try:
        findings=stage.enrich_conversation(ACCOUNT,conversation)
        metrics=build_conversation_metrics(ACCOUNT,conversation,findings)
        unit=create_enrichment_unit(account_ref=metrics.account_ref,conversation_ref=metrics.conversation_ref,
            input_digest=conversation_digest(raw),config_digest=stage.config_digest,cutoff=clock-timedelta(days=90),
            findings=findings,metrics=metrics,
            analyzer_entries=reuse.conversation_entries(conversation_ref(ACCOUNT,raw['conversation_id'])))
        if unit is None:raise ValueError('enrichment_reference_constructor_rejected')
        return unit
    finally:ACTIVE_REUSE.reset(token)


def query_text(module, fragment):
    tree=ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    return next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant)
                and isinstance(n.value,str) and fragment in n.value)


class EnrichmentReferenceFixture:
    def __init__(self,directory,total,clock,*,probe_index=False,case='dominant-insert'):
        from app.analytics.database import ProjectionsDatabase
        from app.analytics.opaque_refs import account_ref
        from app.analytics import conversation_enrichment_unit_sql as sql
        from app.analytics.conversation_enrichment_units import analyzer_records,ConversationEnrichmentReference
        from tools.analytics_graph_component import insert_generation
        self.directory=directory;directory.mkdir(parents=True,exist_ok=True)
        self.total,self.clock,self.case=total,clock,case
        self.changed_chat=1 if case=='small-append' else 0
        self.operation='insert' if case=='dominant-insert' else 'append'
        self.account=account_ref(ACCOUNT)
        self.old='00000000-0000-0000-0000-000000000061'
        self.new='00000000-0000-0000-0000-000000000062'
        self.seed=directory/'seed.sqlite3'
        self.database=ProjectionsDatabase(self.seed)
        self.shape=[];self.headers=[];self.references=[]
        with self.database.transaction() as db:
            db.execute("INSERT INTO projection_publication_epochs VALUES ('component-epoch','component-owner',?,'open','2026-10-01T00:00:00.000000Z',NULL)",('sha256:'+'a'*64,))
            insert_generation(db,self.old,self.account)
            units=[]
            for chat in range(101):
                raw=raw_for(total,chat,clock)
                unit=unit_from_source(raw,clock)
                sql._validate_unit(unit)
                self.headers.append(unit.header)
                self.shape.append(dict(conversation=chat,messages=unit.header.message_count,
                    message_bytes=len(unit.messages),analyzer_bytes=len(unit.analyzers),
                    analyzer_records=len(analyzer_records(unit)),input_digest=unit.header.input_digest,unit_id=unit.header.unit_id))
                if chat==self.changed_chat:self.previous=unit
                else:self.references.append(ConversationEnrichmentReference(self.old,unit.header))
                units.append(unit)
            if sql.insert_units(db,self.old,units)!=101:raise ValueError('enrichment_fixture_insert_failed')
            del units
            self._pending(db,self.old)
            db.execute('UPDATE projection_generations SET status=\'active\',activated_at=? WHERE generation_id=?',(self.timestamp,self.old))
        self.changed=unit_from_source(raw_for(total,self.changed_chat,clock,self.operation),clock)
        sql._validate_unit(self.changed)
        self.current_headers=tuple(self.changed.header if h.conversation_ref==self.changed.header.conversation_ref else h for h in self.headers)
        with self.database.read() as db:
            if probe_index:
                if db.execute('PRAGMA user_version').fetchone()[0]!=24:raise ValueError('index_probe_requires_catalog24')
                at=time.monotonic();db.execute(INDEX_SQL);db.commit();self.index_creation_seconds=time.monotonic()-at
            else:self.index_creation_seconds=None
            self.configuration={key:db.execute('PRAGMA '+key).fetchone()[0] for key in
                ('page_size','page_count','journal_mode','synchronous','cache_size','foreign_keys','user_version')}
            self.configuration['indexes']=[list(r) for r in db.execute('PRAGMA index_list(conversation_enrichment_units)')]
            self.configuration['trigger_sha256']=hashlib.sha256(json.dumps([list(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")],separators=(',',':')).encode()).hexdigest()
        self.database.release_wal_anchor()
        if self.seed.with_name(self.seed.name+'-wal').exists() and self.seed.with_name(self.seed.name+'-wal').stat().st_size:
            raise ValueError('enrichment_seed_not_closed')
        self.seed_hash=hashlib.sha256(self.seed.read_bytes()).hexdigest()
        self.metadata=dict(account_messages_basis=total,conversations=101,case=case,shape=self.shape,
            changed_unit_id=self.changed.header.unit_id,unchanged_references=100,
            source_kind='Qualification-shaped synthetic source; ordinary analyzers and real bounded analyzer-cache entries.',
            proof_scope='Independently validated enrichment headers bound for this SQL-only fixture, not canonical publication authority.')

    @property
    def timestamp(self):return self.clock.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    def _pending(self,db,generation):
        db.execute("UPDATE projection_generations SET status='validated' WHERE generation_id=?",(generation,))
        db.execute("UPDATE projection_generations SET status='activation_pending',activation_intent_id='component-witness',witness_sequence=1 WHERE generation_id=?",(generation,))

    def copy_database(self,label):
        from app.analytics.database import ProjectionsDatabase
        path=self.directory/label/'analytics.sqlite3';path.parent.mkdir()
        shutil.copy2(self.seed,path)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=self.seed_hash:raise ValueError('fixture_copy_hash_differs')
        return ProjectionsDatabase(path)

    def plans(self):
        from app.analytics import enrichment_proof_transition as ep, conversation_enrichment_unit_sql as sql
        database=self.copy_database('query-plan-only')
        try:
            with database.read() as db:
                objects={r['rootpage']:r['name'] for r in db.execute("SELECT name,rootpage FROM sqlite_master WHERE type IN ('table','index')")}
                copy=query_text(sql,'SELECT ?,creator_account_id,conversation_ref,?,input_digest')
                h=self.references[0].header
                args=(self.new,1,self.old,self.account,h.conversation_ref,h.input_digest,h.config_digest,h.unit_id)
                select=query_text(ep,'SELECT r.creator_account_id,r.conversation_ref,r.unit_id,r.ordinal,')
                return {name:dict(eqp=[list(r) for r in db.execute('EXPLAIN QUERY PLAN '+text,values)],
                    program=[list(r) for r in db.execute('EXPLAIN '+text,values)],
                    opened_objects=[dict(opcode=r[1],object=objects.get(r[3]),database=r[4]) for r in db.execute('EXPLAIN '+text,values) if r[1] in ('OpenRead','OpenWrite')])
                    for name,text,values in [('copy',copy,args),('references',select,(self.old,))]}
        finally:database.release_wal_anchor()

    def verify(self,database):
        from app.analytics import conversation_enrichment_unit_sql as sql
        from app.analytics import conversation_enrichment_units as units
        from app.analytics.opaque_refs import conversation_ref
        actual=[]
        ordered_headers=[self.changed.header,*[r.header for r in self.references]]
        expected_refs=sorted((self.new,h.account_ref,h.conversation_ref,i,h.input_digest,h.config_digest,
            h.retention_cutoff.isoformat(),h.expires_at.isoformat(),h.unit_id) for i,h in enumerate(ordered_headers))
        with database.read() as db:
            refs=[tuple(r) for r in db.execute('SELECT * FROM conversation_enrichment_refs WHERE generation_id=? ORDER BY conversation_ref',(self.new,))]
            if len(refs)!=101 or refs!=expected_refs:raise ValueError('enrichment_final_reference_selection')
            for chat in range(101):
                # Recompute messages and metrics from declared source afresh.
                # Separately validate actual cached analyzer records and all digests.
                expected=unit_from_source(raw_for(self.total,chat,self.clock,self.operation if chat==self.changed_chat else None),self.clock,retain_analyzers=False)
                stored=sql.load_unit(db,self.new,self.account,conversation_ref(ACCOUNT,f'chat-{chat}'))
                if stored is None:raise ValueError('enrichment_actual_unit_missing')
                sql._validate_unit(stored)
                adjusted=replace(stored.header,analyzer_digest=expected.header.analyzer_digest,unit_id=expected.header.unit_id)
                if adjusted!=expected.header or units.message_frame(stored)!=units.message_frame(expected):
                    raise ValueError('enrichment_source_recomputation_differs')
                actual.append((stored.header.conversation_ref,stored.header.unit_id,
                    hashlib.sha256(stored.messages+stored.analyzers).hexdigest()))
            if db.execute('SELECT 1 FROM projection_generations WHERE generation_id=?',(self.old,)).fetchone():raise ValueError('retired_generation_remains')
            if db.execute('SELECT 1 FROM conversation_enrichment_units WHERE creator_account_id=? AND unit_id=?',(self.account,self.previous.header.unit_id)).fetchone():raise ValueError('unshared_enrichment_remains')
            if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('enrichment_foreign_keys_invalid')
            if tuple(db.execute('SELECT page_retirement,graph_retirement FROM generation_content_bulk_cleanup').fetchone())!=(0,0):raise ValueError('retirement_scope_open')
        return hashlib.sha256(json.dumps((refs,actual),separators=(',',':')).encode()).hexdigest()

    def sample(self,index,trace):
        from app.analytics import conversation_enrichment_unit_sql as sql
        from app.analytics.database import generation_verification_cache,generation_retirement_cache
        from app.analytics.enrichment_proof_transition import capture_transition,finish_transition
        from app.analytics.conversation_enrichment_units import ConversationEnrichmentProof
        from app.analytics.validation_receipt import content_stamp,generation_binding
        from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore as Store
        from tools.analytics_graph_component import insert_generation
        reset=time.monotonic();database=self.copy_database(f'sample-{index:02}')
        reset_seconds=time.monotonic()-reset;times={};trace.phase='enrichment-reference-'+str(index)
        def measured(name,fn):
            at=time.monotonic();cpu=time.thread_time()
            with trace.span('enrichment-reference.'+name):result=fn()
            times[name]=dict(seconds=time.monotonic()-at,thread_cpu_seconds=time.thread_time()-cpu)
            return result
        proof=None
        try:
            start=time.monotonic()
            # Normal transaction scopes include their connection acquisition,
            # actual commit, close and restoration. Smaller spans are nested.
            def store():
                nonlocal proof
                with database.transaction() as db,generation_verification_cache(db):
                    insert_generation(db,self.new,self.account,self.old)
                    if measured('store_and_copy',lambda:sql.insert_units(db,self.new,[self.changed,*self.references]))!=101:
                        raise ValueError('enrichment_copy_incomplete')
                    self._pending(db,self.new)
                    generation=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(self.new,)).fetchone()
                    proof=ConversationEnrichmentProof(self.new,generation_binding(generation),tuple(content_stamp(db)),self.current_headers)
            measured('store_transaction',store)
            def activate():
                nonlocal proof
                with database.transaction() as db,generation_verification_cache(db):
                    generation=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(self.new,)).fetchone()
                    transition=measured('capture_activation',lambda:capture_transition(db,generation,proof))
                    if transition is None:raise ValueError('activation_selection_not_bound')
                    with generation_retirement_cache(db):
                        measured('retire_old_references',lambda:Store._retire_active_generation(db,self.old,self.account,self.timestamp))
                    db.execute("UPDATE projection_generations SET status='active',activated_at=? WHERE generation_id=?",(self.timestamp,self.new))
                    renewed=measured('finish_activation',lambda:finish_transition(db,transition))
                    if renewed is None:raise ValueError('activation_selection_changed')
                proof=renewed # Never install before commit.
            measured('activation_transaction',activate)
            def collect():
                nonlocal proof
                with database.transaction() as db,generation_retirement_cache(db):
                    generation=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(self.new,)).fetchone()
                    transition=measured('capture_gc',lambda:capture_transition(db,generation,proof))
                    if transition is None:raise ValueError('gc_selection_not_bound')
                    # Reference-SQL component has no graph payloads. Delete the
                    # retired generation through its actual immutable guards.
                    measured('delete_retired_generation',lambda:db.execute("DELETE FROM projection_generations WHERE generation_id=? AND status='retired'",(self.old,)))
                    renewed=measured('finish_gc',lambda:finish_transition(db,transition))
                    if renewed is None:raise ValueError('gc_selection_changed')
                proof=renewed
            measured('gc_transaction',collect)
            proof=None
            transition_seconds=time.monotonic()-start
            at=time.monotonic();digest=self.verify(database);oracle=time.monotonic()-at
            return dict(index=index,intervals=times,transition_seconds=transition_seconds,
                reset_seconds=reset_seconds,oracle_seconds=oracle,output_digest=digest,
                independent_source_and_persisted_equal=True,foreign_keys_valid=True,
                complete_reference_comparisons=4,unchanged_references_copied=100,
                commits=3,shared_units_retained=True,unshared_unit_removed=True,
                retirement_scope_closed=True)
        finally:database.release_wal_anchor()

    def close(self):self.database.release_wal_anchor()


async def run_enrichment_reference(args,q,light,outer,status,manifest,result,workdir):
    from tools.analytics_insertion_diagnostic import Attribution
    from app.analytics.database import GENERATION_VERIFICATION_CACHE_KIB,GENERATION_RETIREMENT_CACHE_KIB
    at=time.monotonic();light.atomic_status(status,'enrichment-reference-preparation')
    fixture=EnrichmentReferenceFixture(workdir,args.messages,
        datetime.fromisoformat(manifest['fixture']['evaluation_clock']),
        probe_index=args.enrichment_index_probe,case=args.enrichment_reference_case)
    trace=Attribution(enabled=args.trace_mode!='none')
    result.update(schema='a07-enrichment-reference-sql.v1',fixture=fixture.metadata,
        preparation_seconds=time.monotonic()-at,configuration=fixture.configuration,
        index_creation_seconds=fixture.index_creation_seconds,seed_bytes=fixture.seed.stat().st_size,
        query_plans=fixture.plans(),samples=[],
        cache_scopes=dict(store_validation_kib=GENERATION_VERIFICATION_CACHE_KIB,retirement_kib=GENERATION_RETIREMENT_CACHE_KIB),
        experimental_index=args.enrichment_index_probe,
        scope='Actual enrichment constructors, SQL and ordered reference checks. Three committed write transactions per sample. Independent source/byte checks per sample. No canonical publication, full graph, retention backlog or scheduler qualification.')
    try:
        for index in range(args.focused_repeats):
            light.atomic_status(status,'enrichment-reference-sample',index=index)
            sample=fixture.sample(index,trace);result['samples'].append(sample)
            q.write_once(args.output/'samples'/f'{index:02}.json',sample)
        result['complete']=True
    finally:
        trace.restore();fixture.close();result['attribution']=trace.events
        result['safety']=dict(component_database_closed=True,scheduler_used=False,publication_authority_tested=False)
    result['summary']=dict(samples=len(result['samples']),references=101)
