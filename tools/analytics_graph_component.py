"""Graph-transition component of the existing light diagnostic, not publication.

The fixture supplies graph inputs and complete predecessor proofs explicitly.
It cannot establish canonical authority, idle lifecycle or scheduler visibility.
Independent elapsed durations use perf_counter for short Windows operations;
Attribution spans retain their own monotonic trace coordinate clock.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import groupby, islice
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import hashlib
import inspect
import json
import time

from tools import analytics_insertion_diagnostic as enrichment


def _graph(conversations, findings, metrics):
    from app.analytics.compact_graph import CompactGraph
    from app.analytics.graph_projection import RelationshipGraphProjector
    from tests.continuous_analytics_fixture import ACCOUNT
    from app.analytics.opaque_refs import account_ref
    graph = CompactGraph(account_ref(ACCOUNT))
    for nodes, edges in RelationshipGraphProjector().batches(ACCOUNT, 2, conversations, findings, metrics):
        graph.add(nodes, edges, check=lambda: None)
    return graph


def graph_inputs(total, clock):
    from app.analytics.conversation_enrichment_units import message_records
    from app.models.analytics import CanonicalConversation, MessageEnrichment
    from tests.continuous_analytics_fixture import ACCOUNT
    raw = enrichment.raw_fixture(total, clock)
    previous = enrichment.full_unit(raw, ACCOUNT, clock-timedelta(days=90))
    new_raw = enrichment.mutate_raw(raw, 'insert')
    current = enrichment.full_unit(new_raw, ACCOUNT, clock-timedelta(days=90))
    rows = message_records(previous); new_rows = message_records(current)
    conv = CanonicalConversation.model_validate(raw)
    findings = [MessageEnrichment.model_validate_json(row) for row in rows]
    dominant = _graph([conv], findings, [previous.header.metrics])
    conversations, all_findings, metrics = [conv], list(findings), [previous.header.metrics]
    # Keep the account's actual 101-conversation bucket density, not just half
    # the graph. The layout, IDs and source times match the qualification fixture.
    for chat in range(1, 101):
        selected=[]
        for index in range(total//2+chat-1,total,100):
            selected.append(dict(message_id=f'matrix-input-{index}',source_ordinal=len(selected),
                text='Thanks pricing',sent_at=(clock-timedelta(hours=48)+timedelta(seconds=index*48*3600/total)).isoformat(),
                direction='inbound' if index%2==0 else 'outbound',sentiment=None))
        if not selected: continue
        other=dict(raw,conversation_id=f'chat-{chat}',messages=selected,last_message_at=selected[-1]['sent_at'])
        unit=enrichment.full_unit(other,ACCOUNT,clock-timedelta(days=90))
        conversations.append(CanonicalConversation.model_validate(other));metrics.append(unit.header.metrics)
        all_findings.extend(MessageEnrichment.model_validate_json(row) for row in message_records(unit))
    account=_graph(conversations,all_findings,metrics)
    from app.analytics.conversation_insertion import suffix_graph
    from app.analytics.graph_projection import RelationshipGraphProjector
    pipeline=SimpleNamespace(graph_projector=RelationshipGraphProjector())
    start=len(rows)-2
    before=suffix_graph(pipeline,ACCOUNT,3,raw,rows,previous.header.metrics,start,lambda:None,None)
    after=suffix_graph(pipeline,ACCOUNT,3,new_raw,new_rows,current.header.metrics,start,lambda:None,None)
    from app.analytics.compact_graph import CompactGraph
    updated=CompactGraph(account.account_ref)
    for name in ('nodes','edges'):
        values=dict(getattr(account,name))
        old,new=getattr(before,name),getattr(after,name)
        for key in old.keys()-new.keys(): values.pop(key)
        values.update(new);setattr(updated,name,values)
    return raw,new_raw,previous,current,dominant,account,updated,before,after


def plans(graph, existing=None, *, retain=None):
    from app.analytics.shared_graph import _plans, _segment_chunk, SegmentValidation, VerifiedSegment
    entries=[];verified=[]
    for plan in _plans(graph,existing or {},lambda:None):
        identity=str(UUID(hashlib.sha256((plan.kind+plan.bucket+plan.digest).encode()).hexdigest()[:32]))
        plan=replace(plan,segment_id=identity)
        encoded,digest=_segment_chunk(graph,plan,lambda:None)
        field='kind' if plan.kind=='node' else 'relation'
        records=graph.nodes if plan.kind=='node' else graph.edges
        categories=Counter(json.loads(records[key])[field] for key in plan.keys)
        entries.append((plan,encoded if retain is None or (plan.kind,plan.bucket) in retain else None,digest))
        verified.append(VerifiedSegment(plan.kind,plan.bucket,identity,plan.digest,len(plan.keys),tuple(sorted(categories.items())),digest))
    return entries,tuple(verified)


def insert_generation(db, name, account, previous=None):
    stamp='2026-10-01T00:00:00.000000Z';digest='sha256:'+'a'*64
    row=dict(generation_id=name,creator_account_id=account,status='building',schema_version=3,
        build_version='component-fixture',canonical_revision=2 if previous is None else 3,
        canonical_content_digest=digest,canonical_high_water_json='{}',pipeline_revision='component-fixture',
        pipeline_config_digest=digest,pipeline_identity_digest=digest,projection_digest=digest,graph_digest=digest,
        node_count=0,edge_count=0,publication_epoch='component-epoch',owner_id='component-owner',owner_pid=1,
        owner_process_started_at=stamp,owner_instance_nonce='component',owner_capability_digest=digest,
        lease_expires_at=stamp,started_at=stamp,validated_at=stamp,expected_active_generation_id=previous,
        expected_active_revision=2 if previous else None)
    db.execute('INSERT INTO projection_generations('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))


def persist_segments(db, graph, entries, generation, needed, old=None):
    from app.analytics import shared_graph, graph_membership_pages as pages
    old=old or {};account=graph.account_ref
    for plan,encoded,digest in entries:
        if not plan.reused:
            db.execute('INSERT INTO graph_segments(creator_account_id,segment_id,kind,bucket,content_digest) VALUES (?,?,?,?,?)',
                (account,plan.segment_id,plan.kind,plan.bucket,plan.digest))
        db.execute('INSERT INTO generation_graph_segments VALUES (?,?,?,?,?)',
            (generation,account,plan.kind,plan.bucket,plan.segment_id))
        if plan.reused:continue
        if (plan.kind,plan.bucket) in needed:
            records=graph.nodes if plan.kind=='node' else graph.edges
            page_map=pages.prepare_pages(db,account,plan.segment_id,plan.kind,records,plan.keys,old.get((plan.kind,plan.bucket)),check=lambda:None)
            db.execute('INSERT INTO graph_segment_chunks(creator_account_id,segment_id,kind,record_count,canonical_bytes,canonical_digest) VALUES (?,?,?,?,?,?)',
                (account,plan.segment_id,plan.kind,len(plan.keys),encoded,digest))
            fields=('node_id,kind,occurred_at,properties_json' if plan.kind=='node' else
                'edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json')
            stream=iter(shared_graph._records(graph,plan,lambda:None))
            while True:
                batch=list(islice(stream,256))
                if not batch:break
                if plan.kind=='edge':
                    endpoints={key for value,_ in batch for key in value[3:5]}
                    db.executemany('INSERT OR IGNORE INTO graph_node_identities VALUES (?,?)',[(account,key) for key in endpoints])
                db.executemany(f'INSERT INTO graph_{plan.kind}_content(creator_account_id,content_id,{fields}) VALUES ('+
                    ','.join('?' for _ in batch[0][0])+') ON CONFLICT(creator_account_id,content_id) DO NOTHING',[value for value,_ in batch])
                pages.write_members(db,plan.kind,[member for _,member in batch],{(plan.kind,plan.segment_id):page_map})
        db.execute('UPDATE graph_segments SET sealed=1 WHERE creator_account_id=? AND segment_id=?',(account,plan.segment_id))


class GraphFixture:
    def __init__(self, directory, total, clock):
        from app.analytics.database import ProjectionsDatabase
        from app.analytics import conversation_graph_units as units
        from app.analytics import conversation_graph_unit_sql as sql
        from app.analytics import shared_graph
        from app.analytics.validation_receipt import content_stamp,generation_binding
        from tests.continuous_analytics_fixture import ACCOUNT
        from app.analytics.source_snapshot import conversation_digest
        values=graph_inputs(total,clock)
        raw,new_raw,previous,current,dominant,account,updated,self.before,self.after=values
        self.account=account.account_ref;self.previous_enrichment=previous;self.current=current
        self.input_digest=conversation_digest(new_raw)
        self.findings=[SimpleNamespace(sent_at=previous.header.first_source_at)]
        self.previous=units.create_graph_unit(account_ref=self.account,conversation_ref=previous.header.conversation_ref,
            input_digest=previous.header.input_digest,config_digest=previous.header.config_digest,
            cutoff=previous.header.retention_cutoff,findings=self.findings,metrics=previous.header.metrics,
            graph=dominant,checksum_version=2)
        # Independent full projector input, not constructor output, supplies the
        # component oracle graph. Every sample recomputes its complete unit/root.
        from app.models.analytics import CanonicalConversation, MessageEnrichment
        from app.analytics.conversation_enrichment_units import message_records
        self.oracle=_graph([CanonicalConversation.model_validate(new_raw)],
            [MessageEnrichment.model_validate_json(row) for row in message_records(current)], [current.header.metrics])
        touched={(kind,key[3:5]) for kind in ('node','edge') for key in
            set(getattr(self.before,kind+'s'))|set(getattr(self.after,kind+'s'))}
        old_entries,old_verified=plans(account,retain=touched)
        existing={(p.kind,p.bucket):(p.digest,p.segment_id) for p,_,_ in old_entries}
        new_entries,new_verified=plans(updated,existing,retain=touched)
        self.database=ProjectionsDatabase(directory/'graph.sqlite3')
        self.db=self.database.connect();db=self.db
        db.execute('BEGIN')
        db.execute('INSERT INTO projection_publication_epochs VALUES (?,?,?,?,?,NULL)',
            ('component-epoch','component-owner','sha256:'+'a'*64,'open','2026-10-01T00:00:00.000000Z'))
        self.old_id='00000000-0000-0000-0000-000000000001'
        self.new_id='00000000-0000-0000-0000-000000000002'
        insert_generation(db,self.old_id,self.account)
        persist_segments(db,account,old_entries,self.old_id,touched)
        if sql.insert_units(db,self.old_id,[self.previous])!=1:raise ValueError('component_unit_missing')
        db.execute("UPDATE projection_generations SET status='validated' WHERE generation_id=?",(self.old_id,))
        db.execute("UPDATE projection_generations SET status='activation_pending',activation_intent_id='component-witness',witness_sequence=1 WHERE generation_id=?",(self.old_id,))
        db.execute("UPDATE projection_generations SET status='active',activated_at='2026-10-01T00:00:00.000000Z' WHERE generation_id=?",(self.old_id,))
        insert_generation(db,self.new_id,self.account,self.old_id)
        persist_segments(db,updated,new_entries,self.new_id,{(p.kind,p.bucket) for p,_,_ in new_entries if not p.reused},
            {(p.kind,p.bucket):p.segment_id for p,_,_ in old_entries})
        db.commit();db.execute('BEGIN')
        stored=sql.load_unit(db,self.old_id,self.account,self.previous.header.conversation_ref)
        if stored is None or stored.header!=self.previous.header or units.graph_unit_ids(stored)!=units.graph_unit_ids(self.previous):
            raise ValueError('component_persisted_predecessor_differs')
        self.previous=stored
        oldrow=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(self.old_id,)).fetchone()
        stamp=content_stamp(db);binding=generation_binding(oldrow)
        if stamp is None:raise ValueError('component_actual_schema_untracked')
        self.graph_proof=shared_graph.GraphSegmentProof(self.old_id,binding,tuple(stamp[:3]),old_verified)
        from app.analytics.conversation_integrity import groups_for_members
        summaries,_=groups_for_members(self.previous)
        self.unit_proof=units.ConversationGraphProof(self.old_id,binding,tuple(stamp[:3]),(self.previous.header,),
            ((self.previous.header.conversation_ref,tuple(sorted(summaries))),))
        old_maps={'node':account.nodes,'edge':account.edges}
        new_maps={'node':updated.nodes,'edge':updated.edges}
        validation=[]
        for (p,_,_),v in zip(new_entries,new_verified,strict=True):
            keys=tuple(k for k in p.keys if old_maps[p.kind].get(k)!=new_maps[p.kind][k])
            validation.append(shared_graph.SegmentValidation(p.kind,p.bucket,p.segment_id,p.digest,len(p.keys),p.reused,v.categories,v.chunk_digest,keys))
        self.validation=shared_graph.SharedGraphValidation('sha256:'+'a'*64,tuple(validation),self.graph_proof,
            tuple(sorted(set(account.nodes)-set(updated.nodes))),tuple(sorted(set(account.edges)-set(updated.edges))),
            shared_graph.segment_root_digest(new_verified))
        self.generation=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(self.new_id,)).fetchone()
        self.segments=new_verified
        self.metadata=dict(account_messages_basis=total,predecessor_messages=previous.header.message_count,
            predecessor_nodes=len(dominant.nodes),predecessor_edges=len(dominant.edges),
            account_nodes=len(account.nodes),account_edges=len(account.edges),
            changed_segments=sum(not p.reused for p in validation),
            changed_records=sum(p.count for p in validation if not p.reused),
            changed_keys=sum(len(p.changed_keys) for p in validation),previous_unit_id=self.previous.header.unit_id,
            input_digest=self.input_digest)
        db.rollback()

    def reader(self):
        from app.analytics import shared_graph,conversation_id_frames as frames
        from app.analytics.conversation_append import verified_chunk_content_ids
        cache={};db=self.db
        def opened(kind,bucket):
            key=kind,bucket
            if key not in cache:cache[key]=shared_graph.verified_segment_chunk(db,self.account,self.graph_proof,kind,bucket)
            return cache[key]
        def content(kind,keys,check=lambda:None):
            return shared_graph.selected_content_ids(db,self.old_id,self.account,kind,keys,check,page_layout=True)
        def verified(kind,keys,check=lambda:None):
            result={}
            for bucket,group in groupby(sorted(keys),key=lambda k:k[3:5]):
                value=opened(kind,bucket)
                if value is None:return {}
                result.update(verified_chunk_content_ids(*value,self.account,list(group),check))
            return result
        reader=SimpleNamespace(graph_content_ids=content,append_graph_content_ids=verified)
        if hasattr(frames,'checked_predecessor_groups'):
            reader.insertion_graph_groups=lambda unit,check: frames.checked_predecessor_groups(unit,check) if unit is self.previous else None
        return reader

    def sample(self,trace,index):
        from app.analytics import conversation_graph_insertion as insertion,shared_graph
        from app.analytics import conversation_graph_unit_sql as sql,conversation_graph_units as units
        from app.analytics.conversation_integrity_store import verify_generation_integrity
        from app.analytics.conversation_integrity import groups_for_members
        db=self.db;db.execute('BEGIN');db.execute('SAVEPOINT component_sample')
        selection=None;check=lambda:None;times={};trace.phase='graph-sample-'+str(index)
        def measured(name,fn):
            start=time.perf_counter();cpu=time.thread_time()
            with trace.span('component.graph.'+name): value=fn()
            times[name]=dict(seconds=time.perf_counter()-start,thread_cpu_seconds=time.thread_time()-cpu)
            return value
        transition_started=time.perf_counter()
        selection_rows=selection_bytes=0
        released=False
        try:
            unit,removed=measured('construct',lambda:insertion.replace_suffix(self.reader(),self.previous,self.before,self.after,
                input_digest=self.input_digest,config=self.current.header.config_digest,cutoff=self.current.header.retention_cutoff,
                findings=self.findings,metrics=self.current.header.metrics,check=check))
            if unit is None:raise ValueError('component_construction_fallback')
            measured('store',lambda:sql.insert_units(db,self.new_id,[unit]))
            def prepare():
                if 'selection' not in inspect.signature(shared_graph._verified_changed_segment_chunks).parameters:return None
                from app.analytics.graph_membership_selection import prepare_selection
                return prepare_selection(db,self.generation,self.account,self.unit_proof,self.validation,check)
            selection=measured('prepare_selection',prepare)
            changes=measured('changed_segments',lambda:shared_graph._verified_changed_segment_chunks(db,self.account,self.validation,check,
                **({'selection':selection} if selection is not None else {})))
            if changes is None:raise ValueError('component_changed_segment_fallback')
            verified=measured('integrity',lambda:verify_generation_integrity(db,self.generation,self.account,proof=self.unit_proof,
                graph_validation=self.validation,segments=self.segments,verified_changes=changes,check=check))
            if unit.header not in verified.headers:raise ValueError('component_integrity_missing')
            selection_rows=selection.rows if selection else 0
            selection_bytes=selection.bytes if selection else 0
            measured('release_selection', lambda: selection.discard() if selection is not None else None)
            released=True
            # Include construction, real writes, validation, buffer release and
            # glue, before starting the separately measured independent oracle.
            transition_seconds=time.perf_counter()-transition_started
            enabled=trace.enabled;trace.enabled=False
            try:
                start=time.perf_counter()
                expected=units.create_graph_unit(account_ref=self.account,conversation_ref=self.current.header.conversation_ref,
                    input_digest=self.input_digest,config_digest=self.current.header.config_digest,cutoff=self.current.header.retention_cutoff,
                    findings=self.findings,metrics=self.current.header.metrics,graph=self.oracle,checksum_version=2)
                actual=sql.load_unit(db,self.new_id,self.account,unit.header.conversation_ref)
                if actual is None or actual.header!=expected.header or units.graph_unit_ids(actual)!=units.graph_unit_ids(expected):
                    raise ValueError('component_independent_graph_mismatch')
                groups_for_members(actual)
                oracle_seconds=time.perf_counter()-start
            finally:trace.enabled=enabled
            return dict(index=index,intervals=times,complete_transition_seconds=transition_seconds,
                selected_interval_seconds=sum(times[n]['seconds'] for n in
                ('construct','prepare_selection','changed_segments','integrity')),independent_graph_equal=True,
                persisted_membership_verified=True,oracle_seconds=oracle_seconds,input_digest=self.input_digest,
                output_unit_id=actual.header.unit_id,output_graph_digest=actual.header.graph_digest,
                selected_rows=selection_rows,selected_bytes=selection_bytes,selection_released=released)
        finally:
            if selection is not None and not released:selection.discard()
            db.execute('ROLLBACK TO component_sample');db.execute('RELEASE component_sample');db.rollback()

    def close(self):
        self.db.close()


async def run_graph_component(args,q,light,outer,status,manifest,result,workdir):
    from tools.analytics_insertion_diagnostic import Attribution
    from app.analytics import conversation_graph_insertion,shared_graph,conversation_integrity_store,conversation_append
    workdir.mkdir(parents=True)
    begun=time.perf_counter()
    light.atomic_status(status,'graph-component-preparation')
    fixture=GraphFixture(workdir,args.messages,datetime.fromisoformat(manifest['fixture']['evaluation_clock']))
    result.update(fixture=fixture.metadata,preparation_seconds=time.perf_counter()-begun,samples=[],
        graph_measurement_recipe='a07-graph-transition.v2',
        total_scope='construction + store + selection preparation + independent persisted validation + selection release; excludes separately executed oracle and fixture rollback')
    trace=Attribution(enabled=args.trace_mode!='none')
    counters={'chunk_traversals':0,'chunk_records':0}
    original=conversation_append._checked_record_spans
    def spans(segment,*a,**k):
        if trace.enabled:
            counters['chunk_traversals']+=1;counters['chunk_records']+=segment.count
        yield from original(segment,*a,**k)
    try:
        if trace.enabled:
            trace.patch(conversation_graph_insertion,'replace_suffix')
            trace.patch(shared_graph,'selected_content_ids',lambda a,k:dict(requested_keys=len(a[4])))
            trace.patch(shared_graph,'_verified_changed_segment_chunks')
            trace.patch(conversation_integrity_store,'verify_generation_integrity')
            from app.analytics import conversation_id_frames as frames, graph_membership_selection as selection
            from app.analytics import conversation_graph_unit_sql as storage
            trace.patch(frames, 'checked_predecessor_groups')
            trace.patch(frames, 'canonical_groups')
            trace.patch(frames.IdGroups, 'pack_contract', lambda a,k: dict(records=len(a[0])))
            trace.patch(conversation_graph_insertion, 'create_membership_unit')
            trace.patch(conversation_graph_insertion, 'summarize_group', lambda a,k: dict(records=len(a[4])))
            trace.patch(conversation_graph_insertion, 'encode_manifest')
            trace.patch(conversation_integrity_store, 'summarize_group', lambda a,k: dict(records=len(a[4])))
            trace.patch(conversation_append, 'verified_chunk_content_ids', lambda a,k: dict(
                chunk_records=a[0].count, chunk_bytes=len(a[1]), requested_keys=len(a[3])))
            trace.patch(selection, '_request_frame', lambda a,k: dict(frame_records=a[3],compressed_bytes=len(a[2])))
            trace.patch(selection, 'prepare_selection')
            trace.patch(storage, 'load_unit')
            conversation_append._checked_record_spans=spans
        for index in range(args.focused_repeats):
            light.atomic_status(status,'graph-component-sample',index=index)
            before=dict(counters)
            sample=fixture.sample(trace,index)
            sample['work_counts']={k:counters[k]-before[k] for k in counters}
            result['samples'].append(sample)
            q.write_once(args.output/'samples'/f'{index:02}.json',sample)
        result['complete']=True
    finally:
        conversation_append._checked_record_spans=original
        trace.restore();fixture.close()
        result['attribution']=trace.events
        result['safety']=dict(component_database_closed=True,scheduler_used=False,publication_authority_tested=False)
    result['summary']=dict(samples=len(result['samples']),predecessor_messages=fixture.metadata['predecessor_messages'])
