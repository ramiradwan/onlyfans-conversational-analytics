"""Source-backed topic and literal incidences under one admitted read."""
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionBackpressure, ProjectionUnavailable
from app.analytics.opaque_refs import account_ref, conversation_ref
from app.analytics.query_execution import QuestionCancelled
from app.models.nlp_observations import ObservationLinksRequest, ObservationLinksResponse
from app.nlp.profiles import AnalysisProfile
from tests.continuous_analytics_fixture import ACCOUNT, advance
from tests.test_nlp_read_projection import FixtureAdapter, ready  # noqa: F401
from tests.test_nlp_task_subsets import publish, subset  # noqa: F401

pytestmark = pytest.mark.ci_tier('integration')


def plan(ready, task='message_topics', category='topic.synthetic', **overrides):
    return ObservationLinksRequest.model_validate(dict(expected_generation=ready.projection.projection_generation,
        expected_projection_digest=ready.projection.projection_digest,
        focus=overrides.pop('focus', {'kind':'category','selection':{'task':task,'category_id':category}}), **overrides))


def fetch(ready, **kwargs):
    return ready.resources.observation_links(ready.policy, plan(ready, **kwargs))


def republish(ready, adapter_type):
    document = ready.profile.model_dump(mode='json')
    component = document['components'][0]
    for identifier in ('concept.other','concept.zero'):
        component['tasks'][1]['enabled_ids'].append(identifier)
        component['executed_labels'].append({'task':'concept_mentions','label_id':identifier,'description':'Synthetic type'})
    profile = AnalysisProfile.model_validate_json(json.dumps(document))
    ready.runner.adapter = adapter_type(profile)
    ready.runner.findings.select(ACCOUNT, profile)
    for _ in range(8): assert ready.runner.process_one(ACCOUNT)
    run = ready.runner.publish_pending(ACCOUNT)
    ready.projection = run.artifact.projection


def test_topic_links_keep_population_counts_partial_state_and_no_normalization_access(ready, monkeypatch):
    from app.analytics import nlp_literals
    def forbidden(*args, **kwargs): raise AssertionError('normalization accessed')
    monkeypatch.setattr(nlp_literals,'checked_publication',forbidden)
    old = ready.resources.findings(ready.policy, ready.request)
    result = fetch(ready)
    assert result.tasks == old.tasks
    assert result.graph_digest == old.graph_digest
    assert result.normalization_status == 'not_required'
    assert result.normalization_sha256 is result.normalization_coverage is result.focus_coverage is None
    assert result.support.model_dump() == dict(supporting_conversations=1, supporting_messages=3, occurrences=3,
        complete_supporting_messages=2, partial_supporting_messages=1)
    assert len(result.link_page.items) == 3
    assert {link.assessment for link in result.link_page.items} == {'complete','partial'}
    for link in result.link_page.items:
        assert link.kind == 'message_topic' and link.reference.span is None
        assert link.source.source_ref == link.reference.message_ref
        assert link.provenance.extraction_snapshot_sha256 == result.snapshot_sha256.removeprefix('sha256:')
        assert ready.resources.resolve(ready.policy, link.reference).text.startswith('\U0001f600')


@pytest.mark.parametrize('ready',['categories'],indirect=True)
def test_same_category_id_across_tasks_partial_only_and_zero_hits(ready):
    old = ready.resources.findings(ready.policy, ready.request)
    topic = fetch(ready, category='concept.synthetic')
    mention = fetch(ready, task='concept_mentions', category='concept.synthetic')
    assert topic.tasks == mention.tasks == old.tasks
    assert topic.link_page.total == 1 and mention.link_page.total == 5
    assert topic.link_page.items[0].target.task == 'message_topics'
    assert all(link.target.task == 'concept_mentions' for link in mention.link_page.items)
    partial = fetch(ready, category='topic.partial')
    assert partial.support.occurrences == partial.support.partial_supporting_messages == 1
    assert partial.support.complete_supporting_messages == 0
    zero = fetch(ready, category='topic.zero')
    assert zero.availability == 'available' and zero.link_page.total == zero.support.occurrences == 0
    assert zero.tasks == old.tasks
    with pytest.raises(InvalidAnalyticsRequest): fetch(ready,category='topic.disabled')


def test_literal_links_exact_unicode_spans_counts_and_pinned_pages(ready):
    first = fetch(ready, task='concept_mentions', category='concept.synthetic', evidence_limit=1)
    assert first.link_page.total == first.support.occurrences == 5
    assert first.normalization_coverage == first.focus_coverage
    assert first.normalization_coverage.eligible_occurrences == 5
    second = fetch(ready, task='concept_mentions', category='concept.synthetic', evidence_offset=1, evidence_limit=1,
        expected_view_sha256=first.view_sha256, expected_normalization_sha256=first.normalization_sha256)
    assert first.view_sha256 == second.view_sha256 and first.support == second.support
    assert first.link_page.items[0] != second.link_page.items[0]
    literal = fetch(ready, focus={'kind':'literal','node_id':first.link_page.items[0].target.node_id},
        expected_normalization_sha256=first.normalization_sha256)
    assert literal.support == first.support and literal.focus_coverage == first.focus_coverage
    assert len({(link.reference.message_ref,link.reference.span.start,link.reference.span.end)
                for link in literal.link_page.items}) == 5
    link = next(link for link in literal.link_page.items if link.reference.span.start == 7)
    resolved = ready.resources.resolve(ready.policy,link.reference)
    assert resolved.text[7:11] == 'echo' and (resolved.browser_span.start,resolved.browser_span.end) == (8,12)
    with pytest.raises(InvalidAnalyticsRequest):
        fetch(ready,focus={'kind':'literal','node_id':'sha256:'+'f'*64},expected_normalization_sha256=first.normalization_sha256)
    for changes in ({'direction':'inbound'},{'conversation_ref':conversation_ref(ACCOUNT,'chat-1')},
                    {'start_date':first.link_page.items[0].reference.sent_at}):
        with pytest.raises(ProjectionUnavailable):
            fetch(ready,task='concept_mentions',category='concept.synthetic',expected_view_sha256=first.view_sha256,
                  expected_normalization_sha256=first.normalization_sha256,**changes)


def test_category_focus_never_emits_other_types_from_the_same_message(ready):
    class Mixed(FixtureAdapter):
        def __call__(self, request, cancelled):
            response=json.loads(super().__call__(request,cancelled));number=int(request.items[0].text.rsplit('case',1)[1])
            for finding in response['items'][0]['tasks'][1]['payload']['findings']:
                if number == 1 or finding['span']['start'] == 7: finding['concept_id']='concept.other'
            return json.dumps(response).encode()
    republish(ready,Mixed)
    first=fetch(ready,task='concept_mentions',category='concept.synthetic',evidence_limit=1)
    second=fetch(ready,task='concept_mentions',category='concept.synthetic',evidence_limit=1,evidence_offset=1,
        expected_normalization_sha256=first.normalization_sha256,expected_view_sha256=first.view_sha256)
    other=fetch(ready,task='concept_mentions',category='concept.other')
    assert first.link_page.total == 2 and other.link_page.total == 3
    assert first.normalization_coverage.eligible_occurrences == 5
    assert first.focus_coverage.eligible_occurrences == 2
    assert {first.link_page.items[0].target.category_id,second.link_page.items[0].target.category_id} == {'concept.synthetic'}
    assert first.link_page.items[0].target.type_id != other.link_page.items[0].target.type_id
    assert first.link_page.items[0].target.node_id != other.link_page.items[0].target.node_id
    assert fetch(ready,task='concept_mentions',category='concept.zero').support.occurrences == 0


def test_empty_and_long_keys_are_accounted_without_fabricated_links(ready):
    with ready.fixture.repositories.database.transaction() as db:
        db.execute('UPDATE account_messages SET text=? WHERE message_id=?', ('\U0001f600      echo case0','m-0-0'))
        db.execute('UPDATE account_messages SET text=? WHERE message_id=?', ('\U0001f600 '+ 'z'*257+' case1','m-0-1'))
        advance(db)
    class Unrepresentable(FixtureAdapter):
        def __call__(self, request, cancelled):
            response=json.loads(super().__call__(request,cancelled));number=int(request.items[0].text.rsplit('case',1)[1])
            if number == 1:
                response['items'][0]['tasks'][1]['payload']['findings']=[{'concept_id':'concept.synthetic','span':{'start':2,'end':259}}]
            return json.dumps(response).encode()
    republish(ready,Unrepresentable)
    result=fetch(ready,task='concept_mentions',category='concept.synthetic')
    assert result.normalization_coverage.model_dump() == dict(eligible_occurrences=4,mapped_occurrences=2,
        unrepresentable_occurrences=2,supporting_messages=3)
    assert result.focus_coverage == result.normalization_coverage
    assert result.link_page.total == result.support.occurrences == 2
    assert (result.support.complete_supporting_messages,result.support.partial_supporting_messages)==(1,1)
    canonical=ready.resources.findings(ready.policy,{'expected_generation':ready.projection.projection_generation,
        'expected_projection_digest':ready.projection.projection_digest})
    assert next(task for task in canonical.tasks if task.task=='concept_mentions').rows[0].occurrences == 4


def test_refusals_never_masquerade_as_empty_success_or_replace_pins(ready,monkeypatch):
    from app.analytics import nlp_literals
    from app.analytics.nlp_literals import LiteralIntegrityError,LiteralUnavailable
    first=fetch(ready,task='concept_mentions',category='concept.synthetic')
    for error in (LiteralUnavailable('capacity'),LiteralUnavailable('not_prepared')):
        monkeypatch.setattr(nlp_literals,'checked_publication',lambda *a,**kw: (_ for _ in ()).throw(error))
        result=fetch(ready,task='concept_mentions',category='concept.synthetic')
        assert result.availability=='unavailable' and result.unavailable_reason==error.reason
        assert result.normalization_status=='unavailable' and result.link_page.total==0
        assert result.tasks==first.tasks
        with pytest.raises(ProjectionUnavailable):
            fetch(ready,task='concept_mentions',category='concept.synthetic',expected_normalization_sha256=first.normalization_sha256)
    monkeypatch.setattr(nlp_literals,'checked_publication',lambda *a,**kw: (_ for _ in ()).throw(LiteralIntegrityError()))
    with pytest.raises(LiteralIntegrityError): fetch(ready,task='concept_mentions',category='concept.synthetic')


def test_direction_conversation_windows_and_exact_identity_not_page_size(ready):
    first=fetch(ready,evidence_limit=1)
    second=fetch(ready,evidence_offset=1,evidence_limit=2,expected_view_sha256=first.view_sha256)
    assert second.view_sha256==first.view_sha256 and second.support==first.support
    assert fetch(ready,direction='inbound').support.supporting_messages==2
    assert fetch(ready,direction='outbound').support.supporting_messages==1
    empty=fetch(ready,conversation_ref=conversation_ref(ACCOUNT,'chat-1'))
    assert empty.link_page.total==0 and empty.tasks[0].eligible_messages==4
    at=first.link_page.items[0].reference.sent_at
    assert fetch(ready,start_date=at,end_date=at).tasks[0].eligible_messages==1
    with pytest.raises(ProjectionUnavailable): fetch(ready,expected_view_sha256='sha256:'+'f'*64)
    with pytest.raises(InvalidAnalyticsRequest): fetch(ready,evidence_offset=4,expected_view_sha256=first.view_sha256)


def test_wire_rejects_forged_source_target_and_provenance(ready):
    result=fetch(ready)
    for field,value in (('assessment','uncertain'),('kind','literal_occurrence')):
        body=json.loads(result.model_dump_json());body['link_page']['items'][0][field]=value
        with pytest.raises(ValidationError): ObservationLinksResponse.model_validate_json(json.dumps(body))
    for path,value in ((('source','source_version'),'f'*64),(('provenance','extraction_snapshot_sha256'),'f'*64),
                       (('target','category_id'),'topic.second'),(('provenance','component_sha256'),'f'*64)):
        body=json.loads(result.model_dump_json());body['link_page']['items'][0][path[0]][path[1]]=value
        with pytest.raises(ValidationError): ObservationLinksResponse.model_validate_json(json.dumps(body))
    literal=fetch(ready,task='concept_mentions',category='concept.synthetic')
    for field,value in (('type_id','0'*64),('literal_key',' echo ')):
        body=json.loads(literal.model_dump_json());body['link_page']['items'][0]['target'][field]=value
        with pytest.raises(ValidationError): ObservationLinksResponse.model_validate_json(json.dumps(body))


def test_independent_task_absence_is_explicit_without_normalization(subset,monkeypatch):
    from app.analytics import nlp_literals
    original, baseline=publish(subset)
    _,_,profile,_,resources,policy,_=subset
    if any(binding.task=='message_topics' for binding in profile.bindings):
        category=next(task for task in original.tasks if task.task=='message_topics').rows[0].category_id
    else:
        category='topic.synthetic'
    monkeypatch.setattr(nlp_literals,'checked_publication',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('normalization accessed')))
    request=ObservationLinksRequest(expected_generation=baseline.expected_generation,
        expected_projection_digest=baseline.expected_projection_digest,
        focus={'kind':'category','selection':{'task':'message_topics','category_id':category}})
    result=resources.observation_links(policy,request)
    assert result.tasks==original.tasks and result.task_availability==original.task_availability
    assert result.normalization_status=='not_required'
    if not any(binding.task=='message_topics' for binding in profile.bindings):
        assert result.availability=='unavailable' and result.unavailable_reason=='task_not_declared'
        assert result.link_page.total==0 and result.support.occurrences==0


@pytest.mark.parametrize('deadline',[False,True])
def test_late_cancel_or_deadline_clears_bound_evidence(ready,monkeypatch,deadline):
    from app.analytics import nlp_observation_read
    from app.analytics.query_execution import QuestionLimitExceeded
    original=nlp_observation_read.aggregate
    clear=ready.resources.evidence.clear_account
    cleared=[];cancelled=[False]
    def clearing(policy):
        cleared.append(policy);return clear(policy)
    def stop(*args,**kwargs):
        response=original(*args,**kwargs)
        if deadline: args[5]._deadline=-1
        else: cancelled[0]=True
        return response
    monkeypatch.setattr(nlp_observation_read,'aggregate',stop)
    monkeypatch.setattr(ready.resources.evidence,'clear_account',clearing)
    with pytest.raises(QuestionLimitExceeded if deadline else QuestionCancelled):
        ready.resources.observation_links(ready.policy,plan(ready),cancellation_check=lambda:cancelled[0])
    assert cleared==[ready.policy]


def test_source_edit_retention_and_normalization_revocation_clear_evidence(ready,monkeypatch):
    from app.analytics import nlp_literals,nlp_observation_read
    original=nlp_literals.assert_publication
    def revoke(connection,binding,publication_id,**kwargs):
        with ready.fixture.stores.database.transaction() as db:
            db.execute('DELETE FROM nlp_literal_publication_batches WHERE account_ref=? AND ordinal=0',(account_ref(ACCOUNT),))
        return original(connection,binding,publication_id,**kwargs)
    monkeypatch.setattr(nlp_literals,'assert_publication',revoke)
    with pytest.raises(ProjectionUnavailable): fetch(ready,task='concept_mentions',category='concept.synthetic')
    monkeypatch.setattr(nlp_literals,'assert_publication',original)
    aggregate=nlp_observation_read.aggregate
    def expire(*args,**kwargs):
        result=aggregate(*args,**kwargs)
        monkeypatch.setattr(ready.resources,'clock',lambda:ready.fixture.clock.now+timedelta(days=1000))
        return result
    monkeypatch.setattr(nlp_observation_read,'aggregate',expire)
    with pytest.raises(ProjectionUnavailable): fetch(ready)
    monkeypatch.setattr(nlp_observation_read,'aggregate',aggregate)
    monkeypatch.setattr(ready.resources,'clock',lambda:ready.fixture.clock.now)
    with ready.fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='edited' WHERE message_id='m-0-0'");advance(db)
    with pytest.raises(ProjectionUnavailable): fetch(ready)


def test_shared_slots_cancellation_and_no_persistent_reads(ready):
    from app.analytics.validation_receipt import content_stamp
    with pytest.raises(QuestionCancelled):
        ready.resources.observation_links(ready.policy,plan(ready),cancellation_check=lambda:True)
    assert ready.resources._slots.acquire(False) and ready.resources._slots.acquire(False)
    try:
        with pytest.raises(ProjectionBackpressure): fetch(ready)
    finally:
        ready.resources._slots.release();ready.resources._slots.release()
    with ready.fixture.stores.database.read() as db: before=content_stamp(db)
    fetch(ready)
    with ready.fixture.stores.database.read() as db: assert content_stamp(db)==before
