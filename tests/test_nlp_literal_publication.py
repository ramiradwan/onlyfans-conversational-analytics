"""Literal publication, bounded reuse and source lifecycle over invented findings."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.identity import source_identity
from app.analytics.nlp_findings_store import FrozenFindings
from app.analytics import nlp_literals as literals
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionCancelled
from app.analytics.errors import ProjectionUnavailable
from app.nlp import normalization as n, v2
from app.persistence.retention import CreatorVaultRetention
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_nlp_processing_publication import fixture, runner, ConformanceAdapter

pytestmark = pytest.mark.ci_tier('integration')
TEXT = '\U0001f600 cafe\u0301 caf\u00e9 Caf\u00e9 ' + 'x' * 257 + '  '


class LiteralAdapter(ConformanceAdapter):
    outcome = 'assessed_findings'
    def __call__(self, request, cancelled):
        value = json.loads(super().__call__(request,cancelled))
        payload = next(t['payload'] for t in value['items'][0]['tasks'] if t['payload']['task']=='concept_mentions')
        text = request.items[0].text
        spans = [(text.index(word),text.index(word)+len(word)) for word in ('cafe\u0301','caf\u00e9','Caf\u00e9','x'*257)]
        spans.append((len(text)-2,len(text)))
        payload.update(outcome=self.outcome,findings=[])
        if self.outcome in ('assessed_findings','partial'):
            if self.outcome == 'partial':
                payload['coverage'].update(examined=[{'start':0,'end':12}],excluded=[{'start':12,'end':len(text)}])
                spans = spans[:2]
            payload['findings']=[{'concept_id':'concept.synthetic','span':{'start':a,'end':b}} for a,b in spans]
        return json.dumps(value).encode()


@pytest.fixture
def ready(fixture):
    with fixture.repositories.database.transaction() as db:
        db.execute('UPDATE account_messages SET text=?',(TEXT,))
    r,profile,_=runner(fixture)
    adapter=LiteralAdapter(profile)
    r.adapter=adapter
    assert r.process_one(ACCOUNT) and r.process_one(ACCOUNT)
    run=r.publish_pending(ACCOUNT)
    assert run.changed
    with fixture.stores.database.read() as db:
        key=db.execute('SELECT snapshot_id FROM nlp_generation_refs WHERE generation_id=?',
                       (fixture.stores.database.active_generation(ACCOUNT).generation_id,)).fetchone()[0]
    return SimpleNamespace(f=fixture,r=r,profile=profile,adapter=adapter,binding=FrozenFindings(account_ref(ACCOUNT),key))


def read(ready):
    with ready.f.stores.database.read() as db:
        return literals.checked_publication(db,ready.binding)


def source_record(ready):
    with ready.f.stores.database.read() as db:
        row=db.execute('SELECT r.record_digest,r.document,s.conversation_id,s.message_id FROM nlp_records r '
            'JOIN nlp_jobs j USING(account_ref,record_digest) JOIN nlp_sources s USING(account_ref,source_ref) LIMIT 1').fetchone()
    original=ready.r._read(ACCOUNT,EvidenceLocation(conversation_id=row[2],message_id=row[3]),ready.r._budget())
    return row[0],v2.read_record(bytes(row[1])),original


def test_unicode_nodes_exact_anchors_dispositions_and_authenticated_counts(ready):
    value=read(ready)
    assert {node.literal_key for node in value.nodes} == {'caf\u00e9','Caf\u00e9'}
    assert (value.envelope.records,value.envelope.occurrences,value.envelope.mapped,value.envelope.unrepresentable)==(2,10,6,4)
    assert len(value.mappings)==6 and len(value.dispositions)==4
    assert {d.reason for d in value.dispositions}=={'empty_key','key_too_long'}
    assert all(m.extraction.extraction_snapshot_sha256==ready.binding.snapshot_id for m in value.mappings)
    assert all(m.type_id==n.literal_type_id('concept_mentions.v1',m.extraction.taxonomy,m.occurrence.category_id) for m in value.mappings)
    assert len({m.occurrence.identity_digest() for m in value.mappings})==6
    for mapping in value.mappings:
        surface=TEXT[mapping.occurrence.span.start:mapping.occurrence.span.end]
        node=next(node for node in value.nodes if node.node_id==mapping.node_id)
        assert n.provisional_literal_node(value.envelope.account_namespace,mapping.type_id,surface)==node
    assert all(r.occurrences==5 and r.coverage.complete for r in value.records)
    with ready.f.stores.database.read() as db:
        literals.assert_publication(db,ready.binding,value.publication_id)
        assert literals.serialized_bytes(db)<literals.MAX_BYTES
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_namespace_and_taxonomy_bound_type_identity_prevent_cross_account_merges(ready):
    first=read(ready)
    ready.r.findings.select('creator.other',ready.profile)
    with ready.f.stores.database.read() as db:
        namespace=db.execute('SELECT namespace FROM nlp_literal_accounts WHERE account_ref=?',(account_ref('creator.other'),)).fetchone()[0]
    original=first.nodes[0]
    other=n.provisional_literal_node(namespace,original.type_id,original.literal_key)
    assert other.node_id!=original.node_id
    taxonomy=first.mappings[0].extraction.taxonomy
    changed=taxonomy.model_copy(update={'revision':'other'})
    assert n.literal_type_id('concept_mentions.v1',taxonomy,'concept.synthetic')!=n.literal_type_id('concept_mentions.v1',changed,'concept.synthetic')
    with pytest.raises(ProjectionUnavailable):
        with ready.f.stores.database.read() as db:
            literals.checked_publication(db,FrozenFindings(account_ref('creator.other'),ready.binding.snapshot_id))


@pytest.mark.parametrize('method',['publish_literals','backfill_literals'])
@pytest.mark.parametrize('binding_kind',['other_account','untyped'])
def test_runner_rejects_misbound_literals_before_source_or_store_access(tmp_path,monkeypatch,method,binding_kind):
    from tests.continuous_analytics_fixture import make_fixture,cleanup
    f=make_fixture(tmp_path,conversations=0,messages=0)
    try:
        other='creator.other'
        with f.repositories.database.transaction() as db:
            db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)',
                       (other,1,f.clock.now.isoformat()))
        r,profile,_=runner(f)
        r.findings.select(other,profile)
        identity=source_identity(f.source,ACCOUNT)
        assert source_identity(f.source,other)==identity
        other_binding=r.findings.freeze(other,identity)
        binding=(other_binding if binding_kind=='other_account' else
                 SimpleNamespace(account_ref=account_ref(ACCOUNT),snapshot_id=other_binding.snapshot_id))
        def unexpected(*args,**kwargs):
            pytest.fail('misbound literal request reached source or storage')
        monkeypatch.setattr(f.source,'open_question_scope',unexpected)
        monkeypatch.setattr(f.stores.database,'read',unexpected)
        monkeypatch.setattr(f.stores.database,'transaction',unexpected)
        with pytest.raises(ValueError,match='^nlp_literal_account_mismatch$'):
            getattr(r,method)(ACCOUNT,binding)
    finally:
        cleanup(f)


def test_new_snapshot_reuses_derivations_without_reading_original_sources(ready,monkeypatch):
    before=read(ready)
    with ready.f.stores.database.read() as db:
        derivations=[tuple(row) for row in db.execute('SELECT * FROM nlp_literal_derivations ORDER BY record_digest')]
    ready.r.findings.mode(ACCOUNT,'paused')
    binding=ready.r.findings.freeze(ACCOUNT,source_identity(ready.f.source,ACCOUNT))
    monkeypatch.setattr(ready.r,'_read',lambda *args: (_ for _ in ()).throw(AssertionError('unexpected source read')))
    ready.r.publish_literals(ACCOUNT,binding)
    with ready.f.stores.database.read() as db:
        after=literals.checked_publication(db,binding)
        assert [tuple(row) for row in db.execute('SELECT * FROM nlp_literal_derivations ORDER BY record_digest')]==derivations
    assert after.publication_id!=before.publication_id
    assert after.nodes==before.nodes
    assert {m.occurrence.identity_digest() for m in after.mappings}=={m.occurrence.identity_digest() for m in before.mappings}
    assert all(m.extraction.extraction_snapshot_sha256==binding.snapshot_id for m in after.mappings)


def test_missing_derivation_requires_explicit_bounded_source_backfill(ready):
    with ready.f.stores.database.transaction() as db:
        db.execute('DELETE FROM nlp_literal_publications')
        db.execute('DELETE FROM nlp_literal_derivations')
    with pytest.raises(literals.LiteralUnavailable):
        ready.r.publish_literals(ACCOUNT,ready.binding)
    assert ready.r.backfill_literals(ACCOUNT,ready.binding,maximum=1)==1
    with pytest.raises(literals.LiteralUnavailable):
        ready.r.publish_literals(ACCOUNT,ready.binding)
    assert ready.r.backfill_literals(ACCOUNT,ready.binding,maximum=1)==1
    ready.r.publish_literals(ACCOUNT,ready.binding)
    assert read(ready).envelope.occurrences==10
    assert len(ready.adapter.requests)==2


def test_missing_authenticated_batch_is_integrity_error_not_empty_success(ready):
    with ready.f.stores.database.transaction() as db:
        db.execute('DELETE FROM nlp_literal_derivation_batches WHERE record_digest=(SELECT min(record_digest) FROM nlp_literal_derivations)')
    with pytest.raises(literals.LiteralIntegrityError):
        read(ready)
    with ready.f.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM projection_generations WHERE status='active'").fetchone()[0]==1


def test_immutable_rows_refuse_replace_and_guard_removal(ready):
    with ready.f.stores.database.transaction() as db:
        with pytest.raises(Exception,match='nlp_literal_replace_blocked'):
            db.execute('INSERT OR REPLACE INTO nlp_literal_accounts SELECT * FROM nlp_literal_accounts')
        db.execute('DROP TRIGGER nlp_literal_derivations_immutable')
    with pytest.raises(literals.LiteralIntegrityError):
        read(ready)


def test_actual_serialized_byte_boundary_includes_keys_and_retained_publication(ready,monkeypatch):
    key,record,source=source_record(ready)
    with ready.f.stores.database.transaction() as db:
        old=literals.serialized_bytes(db)
        db.execute('DELETE FROM nlp_literal_derivations WHERE record_digest=?',(key,))
        current=literals.serialized_bytes(db)
        needed=old-current
        monkeypatch.setattr(literals,'MAX_BYTES',old-1)
        with pytest.raises(literals.LiteralUnavailable) as failure:
            literals.capture_record(db,ready.profile,source,record,key)
        assert failure.value.reason=='capacity'
        assert literals.serialized_bytes(db)==current
        monkeypatch.setattr(literals,'MAX_BYTES',old)
        assert literals.capture_record(db,ready.profile,source,record,key)
        assert literals.serialized_bytes(db)==current+needed
    assert read(ready).envelope.occurrences==10


def test_cancelled_staging_is_not_selected_and_canonical_publication_survives(ready,monkeypatch):
    database=ready.f.stores.database
    with database.transaction() as db: db.execute('DELETE FROM nlp_literal_publications')
    original=database.transaction
    cancelled=False
    @contextmanager
    def transaction(*args,**kwargs):
        nonlocal cancelled
        with original(*args,**kwargs) as db:
            yield db
            building=db.execute("SELECT 1 FROM nlp_literal_publications WHERE state='building'").fetchone()
        if building: cancelled=True
    monkeypatch.setattr(database,'transaction',transaction)
    def check():
        if cancelled: raise QuestionCancelled()
    with pytest.raises(QuestionCancelled):
        literals.publish(database,ready.binding,canonical=source_identity(ready.f.source,ACCOUNT),source_current=lambda:None,check=check)
    with database.read() as db:
        assert not db.execute('SELECT 1 FROM nlp_literal_publications').fetchone()
        assert db.execute("SELECT count(*) FROM projection_generations WHERE status='active'").fetchone()[0]==1


def test_stale_revision_before_seal_is_refused(ready):
    database=ready.f.stores.database
    with database.transaction() as db: db.execute('DELETE FROM nlp_literal_publications')
    def change():
        with database.transaction() as db:
            db.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1')
    with pytest.raises(ProjectionUnavailable):
        literals.publish(database,ready.binding,canonical=source_identity(ready.f.source,ACCOUNT),source_current=change)
    with database.read() as db: assert not db.execute('SELECT 1 FROM nlp_literal_publications').fetchone()


@pytest.mark.parametrize('outcome',['partial','uncertain','assessed_no_match'])
def test_task_states_are_retained_without_fabricated_literal_matches(ready,outcome):
    ready.r.findings.select(ACCOUNT,ready.profile)
    ready.adapter.outcome=outcome
    assert ready.r.process_one(ACCOUNT) and ready.r.process_one(ACCOUNT)
    ready.r.publish_pending(ACCOUNT)
    binding=ready.r.findings.freeze(ACCOUNT,source_identity(ready.f.source,ACCOUNT))
    with ready.f.stores.database.read() as db: value=literals.checked_publication(db,binding)
    assert {record.outcome for record in value.records}=={outcome}
    assert len(value.mappings)==(4 if outcome=='partial' else 0)
    assert not value.dispositions


@pytest.mark.asyncio
async def test_scoped_source_delete_preserves_only_other_valid_support(ready):
    from tests.test_findings_erasure import resources,drain
    original=read(ready)
    CreatorVaultRetention(ready.f.repositories.database,clock=lambda:ready.f.clock.now).delete_message(ACCOUNT,'m-0-0')
    _,_,service=resources(ready.f)
    await drain(service)
    with pytest.raises(ProjectionUnavailable): read(ready)
    ready.r.publish_pending(ACCOUNT)
    binding=ready.r.findings.freeze(ACCOUNT,source_identity(ready.f.source,ACCOUNT))
    with ready.f.stores.database.read() as db: value=literals.checked_publication(db,binding)
    assert value.envelope.account_namespace==original.envelope.account_namespace
    assert value.envelope.records==1 and len(value.mappings)==3
    assert {node.node_id for node in value.nodes}=={node.node_id for node in original.nodes}


@pytest.mark.asyncio
async def test_account_erasure_covers_all_payload_and_rotates_namespace(ready):
    from tests.test_findings_erasure import resources,delete_all,drain
    namespace=read(ready).envelope.account_namespace
    operation=delete_all(ready.f)
    _,_,service=resources(ready.f)
    await drain(service,operation)
    with ready.f.stores.database.read() as db:
        assert all(db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0 for table in literals.TABLES)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    ready.r.findings.select(ACCOUNT,ready.profile)
    with ready.f.stores.database.read() as db:
        assert db.execute('SELECT namespace FROM nlp_literal_accounts').fetchone()[0]!=namespace


@pytest.mark.asyncio
async def test_orphan_namespace_cannot_take_the_empty_erasure_fast_path(fixture):
    from tests.test_findings_erasure import resources,delete_all,drain
    r,profile,_=runner(fixture)
    # Reconstruct an interrupted legacy state with only derived namespace data.
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DELETE FROM nlp_accounts')
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT count(*) FROM nlp_literal_accounts').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==0
    operation=delete_all(fixture)
    _,_,service=resources(fixture)
    await drain(service,operation)
    with fixture.stores.database.read() as db:
        assert all(db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0 for table in literals.TABLES)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_pending_sources_seal_zero_record_publication_without_negative_claim(fixture):
    r,_,_=runner(fixture)
    assert r.publish_pending(ACCOUNT).changed
    binding=r.findings.freeze(ACCOUNT,source_identity(fixture.source,ACCOUNT))
    with fixture.stores.database.read() as db:
        value=literals.checked_publication(db,binding)
    assert value.envelope.records==value.envelope.occurrences==0
    assert value.records==value.nodes==value.mappings==value.dispositions==()


def test_cache_capacity_refusal_preserves_canonical_completion_and_publication(fixture,monkeypatch):
    r,profile,_=runner(fixture)
    monkeypatch.setattr(literals,'MAX_BYTES',0)
    assert r.process_one(ACCOUNT) and r.process_one(ACCOUNT)
    assert r.publish_pending(ACCOUNT).changed
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM projection_generations WHERE status='active'").fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM nlp_literal_publications WHERE state='sealed'").fetchone()


def test_expired_snapshot_revokes_literal_identity_without_changing_findings(ready):
    from app.analytics.nlp_reclamation import expire_generation_input
    with ready.f.stores.database.transaction() as db:
        generation=db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        expire_generation_input(db,generation)
    with pytest.raises(ProjectionUnavailable): read(ready)
    with ready.f.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==2


def test_snapshot_and_record_reclamation_cascade_all_owned_literal_payload(ready):
    from app.analytics.nlp_reclamation import reclaim_orphans
    # A standalone frozen snapshot has no report root after ordinary retirement.
    database=ready.f.stores.database
    ready.r.findings.select(ACCOUNT,ready.profile)
    ready.r.publish_pending(ACCOUNT)
    ready.f.stores.projections.rollback_retention=0
    ready.f.stores.projections.collect_garbage(account_ref(ACCOUNT))
    for _ in range(8): reclaim_orphans(database,account_ref(ACCOUNT))
    with database.read() as db:
        assert not db.execute('SELECT 1 FROM nlp_literal_publications WHERE snapshot_id=?',(ready.binding.snapshot_id,)).fetchone()
        assert db.execute('SELECT count(*) FROM nlp_literal_derivations').fetchone()[0]==0
        assert not db.execute('SELECT 1 FROM nlp_literal_publications p LEFT JOIN nlp_snapshots s USING(account_ref,snapshot_id) WHERE s.snapshot_id IS NULL').fetchone()
        assert not db.execute('SELECT 1 FROM nlp_literal_derivations d LEFT JOIN nlp_records r USING(account_ref,record_digest) WHERE r.record_digest IS NULL').fetchone()
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_changed_original_source_cannot_rebind_a_saved_derivation(ready):
    from app.nlp.contracts import NlpBoundaryError
    key,record,source=source_record(ready)
    with ready.f.stores.database.transaction() as db:
        for replacement in (source.model_copy(update={'text':source.text+'!'}),
                            source.model_copy(update={'sender_id':'other.synthetic'})):
            with pytest.raises(NlpBoundaryError):
                literals.capture_record(db,ready.profile,replacement,record,key)
    assert read(ready).envelope.occurrences==10


def test_prepared_publication_preserves_exact_occurrence_cap(ready,monkeypatch):
    monkeypatch.setattr(literals,'MAX_OCCURRENCES',9)
    with pytest.raises(literals.LiteralUnavailable) as failure: ready.r.publish_literals(ACCOUNT,ready.binding)
    assert failure.value.reason=='capacity'
    with ready.f.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==2


def test_changed_canonical_identity_is_stale_not_optional_integrity(ready):
    current=source_identity(ready.f.source,ACCOUNT)
    stale=SimpleNamespace(revision=current.revision+1,content_digest=current.content_digest)
    with pytest.raises(ProjectionUnavailable):
        literals.publish(ready.f.stores.database,ready.binding,canonical=stale,source_current=lambda:None)
    assert read(ready).envelope.occurrences==10


def test_envelope_capacity_is_distinct_from_malformed_content(monkeypatch):
    from tests.test_nlp_normalization import publication
    manifest=publication().model_copy(update={'mapping_batch_sha256':(),'membership_batch_sha256':()})
    fields=dict(publication=manifest,account_namespace='account.synthetic',
        profile_sha256='a'*64,records=0,occurrences=0,mapped=0,unrepresentable=0,
        content_batch_sha256=())
    envelope=literals._envelope(**fields)
    size=len(n.canonical_bytes(envelope))
    monkeypatch.setattr(literals,'MAX_BATCH_BYTES',size)
    assert literals._envelope(**fields)==envelope
    monkeypatch.setattr(literals,'MAX_BATCH_BYTES',size-1)
    with pytest.raises(literals.LiteralUnavailable) as failure: literals._envelope(**fields)
    assert failure.value.reason=='capacity'
    monkeypatch.setattr(literals,'MAX_BATCH_BYTES',65536)
    hashes=tuple(f'{i:064x}' for i in range(512))
    full=manifest.model_copy(update={'mapping_batch_sha256':hashes})
    with pytest.raises(literals.LiteralUnavailable) as failure:
        literals._envelope(**(fields|dict(publication=full,content_batch_sha256=hashes)))
    assert failure.value.reason=='capacity'
    with pytest.raises(ValueError): literals._envelope(**(fields|dict(mapped=1)))
