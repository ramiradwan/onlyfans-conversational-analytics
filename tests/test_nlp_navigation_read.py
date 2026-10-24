"""Grouped saved reads retain task populations, source fences and exact pages."""
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.nlp_navigation import NavigationRegistry, NavigationAccumulator, read_grouped
from app.analytics.opaque_refs import conversation_ref
from app.analytics.query_execution import QuestionCancelled, QuestionLimitExceeded
from app.analytics.query_runtime import QuestionResources
from app.models.insights import FindingsCategoryFilter, GroupedFindingsRequest, GroupedFindingsResponse
from app.nlp.navigation import CategoryNavigationDescriptor, NavigationGroup, navigation_identity, profile_bindings
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_nlp_read_projection import ready
from tests.test_nlp_task_subsets import subset

pytestmark = pytest.mark.ci_tier('integration')


def descriptor(profile, revision='1'):
    enabled = {binding.task:binding.enabled_ids for binding in profile_bindings(profile)}
    return CategoryNavigationDescriptor(policy_id='navigation.synthetic',revision=revision,
        source_presentation=profile.presentation,bindings=profile_bindings(profile),groups=(
            NavigationGroup(group_id='topics',display_label='Topics',task='message_topics',
                            member_ids=enabled['message_topics']),
            NavigationGroup(group_id='overlap',display_label='Overlapping topics',task='message_topics',
                            member_ids=('topic.synthetic',)),
            NavigationGroup(group_id='mentions',display_label='Mentions',task='concept_mentions',
                            member_ids=enabled['concept_mentions'])))


@contextmanager
def resources(ready, registry):
    value = QuestionResources(ready.fixture.source,ready.fixture.pipeline,
        clock=lambda:ready.fixture.clock.now,findings_navigation=registry)
    try:
        yield value
    finally:
        value.close()
        assert value.wait_closed()


def plan(ready, **updates):
    return GroupedFindingsRequest.model_validate(ready.request.model_dump()|{'view':'grouped'}|updates)


def test_union_support_deduplicates_categories_occurrences_and_keeps_no_match_denominator(ready):
    declaration = descriptor(ready.profile)
    with resources(ready,NavigationRegistry((declaration,))) as owner:
        result = read_grouped(owner,ready.policy,plan(ready))
    flat = ready.resources.findings(ready.policy,ready.request)
    assert result.schema_version=='analytics-findings.v4' and result.view=='grouped'
    assert result.tasks==flat.tasks and result.comparison==flat.comparison
    assert result.evidence_page==flat.evidence_page and result.conversation_page==flat.conversation_page
    assert result.navigation.policy==navigation_identity(declaration)
    assert result.navigation.previous is None
    for group in result.navigation.current:
        assert (group.supporting_messages,group.complete_supporting_messages,group.partial_supporting_messages)==(3,2,1)
        assert group.share_of_complete_messages==2/3
    assert result.tasks[1].rows[0].occurrences==5
    assert result.navigation.current[0].supporting_messages < sum(row.supporting_messages for row in result.tasks[0].rows)


@pytest.mark.parametrize('ready',['categories'],indirect=True)
def test_same_id_tasks_partial_zero_and_category_drilldown_preserve_group_population(ready):
    original = descriptor(ready.profile)
    declaration = original.model_copy(update={'groups':original.groups+(
        NavigationGroup(group_id='collision',display_label='Topic collision',task='message_topics',member_ids=('concept.synthetic',)),
        NavigationGroup(group_id='partial',display_label='Partial',task='message_topics',member_ids=('topic.partial',)),
        NavigationGroup(group_id='zero',display_label='Zero',task='message_topics',member_ids=('topic.zero',)))})
    with resources(ready,NavigationRegistry((declaration,))) as owner:
        full = read_grouped(owner,ready.policy,plan(ready))
        filtered = read_grouped(owner,ready.policy,plan(ready,category_filter=FindingsCategoryFilter(
            task='message_topics',category_id='topic.second'),conversation_limit=1))
        page = read_grouped(owner,ready.policy,plan(ready,evidence_offset=1,evidence_limit=1,
            expected_navigation_sha256='sha256:'+navigation_identity(declaration).sha256))
    assert filtered.navigation==full.navigation and filtered.tasks==full.tasks
    assert filtered.evidence_page.total==1 and filtered.conversation_page.total==1
    rows = {row.group_id:row for row in full.navigation.current}
    assert rows['topics'].supporting_messages==4
    assert rows['collision'].supporting_messages==1 and rows['mentions'].supporting_messages==3
    assert rows['partial'].complete_supporting_messages==0 and rows['partial'].partial_supporting_messages==1
    assert rows['zero'].supporting_messages==0 and rows['zero'].share_of_complete_messages==0
    assert page.evidence_page.items==(full.evidence_page.items[1],)


def test_comparison_uses_same_conversation_direction_and_complete_denominator(ready):
    start = ready.fixture.clock.now-timedelta(days=8)+timedelta(minutes=2)
    with resources(ready,NavigationRegistry((descriptor(ready.profile),))) as owner:
        result = read_grouped(owner,ready.policy,plan(ready,start_date=start,end_date=start+timedelta(minutes=2),
            conversation_ref=conversation_ref(ACCOUNT,'chat-0')))
        point = read_grouped(owner,ready.policy,plan(ready,start_date=start,end_date=start))
        outbound = read_grouped(owner,ready.policy,plan(ready,direction='outbound'))
    assert result.comparison.availability=='available'
    assert all(row.supporting_messages==1 and row.partial_supporting_messages==1 and
               row.share_of_complete_messages is None for row in result.navigation.current)
    assert all(row.supporting_messages==2 and row.complete_supporting_messages==2 and
               row.share_of_complete_messages==1 for row in result.navigation.previous)
    assert point.navigation.previous is None
    assert outbound.navigation.current[0].supporting_messages==1


def test_missing_declaration_and_retained_policy_pin_never_substitute(ready):
    first, second = descriptor(ready.profile), descriptor(ready.profile,'2')
    with resources(ready,NavigationRegistry()) as owner:
        absent = read_grouped(owner,ready.policy,plan(ready))
        assert absent.navigation.availability=='not_declared' and absent.navigation.current==()
        with pytest.raises(ProjectionUnavailable):
            read_grouped(owner,ready.policy,plan(ready,expected_navigation_sha256='sha256:'+navigation_identity(first).sha256))
    with resources(ready,NavigationRegistry((first,second),active_sha256=(navigation_identity(second).sha256,))) as owner:
        current = read_grouped(owner,ready.policy,plan(ready))
        retained = read_grouped(owner,ready.policy,plan(ready,expected_navigation_sha256='sha256:'+navigation_identity(first).sha256))
        assert current.navigation.policy==navigation_identity(second)
        assert retained.navigation.policy==navigation_identity(first)
        with pytest.raises(InvalidAnalyticsRequest):
            read_grouped(owner,ready.policy,plan(ready).model_dump()|{'conversation_offset':1})
        with pytest.raises(ProjectionUnavailable):
            read_grouped(owner,ready.policy,plan(ready,expected_navigation_sha256='sha256:'+'f'*64))


@pytest.mark.parametrize('interruption',[QuestionCancelled,QuestionLimitExceeded])
def test_group_expansion_observes_shared_budget_and_clears_evidence(ready,monkeypatch,interruption):
    def stop(self,*args): raise interruption()
    monkeypatch.setattr(NavigationAccumulator,'observe',stop)
    with resources(ready,NavigationRegistry((descriptor(ready.profile),))) as owner:
        with pytest.raises(interruption): read_grouped(owner,ready.policy,plan(ready))


def test_source_edit_revokes_grouped_response_and_registered_evidence(ready):
    with resources(ready,NavigationRegistry((descriptor(ready.profile),))) as owner:
        response = read_grouped(owner,ready.policy,plan(ready))
        with ready.fixture.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text=text||'changed'")
        with pytest.raises(ProjectionUnavailable): read_grouped(owner,ready.policy,plan(ready))
        assert response.navigation.current


def test_navigation_response_rejects_hash_order_and_denominator_drift(ready):
    with resources(ready,NavigationRegistry((descriptor(ready.profile),))) as owner:
        response = read_grouped(owner,ready.policy,plan(ready))
    for fault in ('hash','order','denominator','previous','presentation'):
        body = response.model_dump(mode='json')
        if fault=='hash': body['navigation']['policy']['sha256']='f'*64
        elif fault=='order': body['navigation']['current'].reverse()
        elif fault=='denominator': body['navigation']['current'][0]['share_of_complete_messages']=0
        elif fault=='previous': body['navigation']['previous']=body['navigation']['current']
        else: body['navigation']['declaration']['source_presentation']['revision']='changed'
        import json
        with pytest.raises(ValueError): GroupedFindingsResponse.model_validate_json(json.dumps(body))


def test_grouping_does_not_consume_source_capacity_twice(ready,monkeypatch):
    from tests.test_nlp_read_projection import test_full_source_capacity_is_counted_once_across_tasks_comparison_and_evidence
    from app.analytics import nlp_read_projection as projection
    original = projection.aggregate
    def grouped(*args,**kwargs):
        return original(*args,**kwargs,navigation=descriptor(ready.profile),grouped=True)
    monkeypatch.setattr(projection,'aggregate',grouped)
    test_full_source_capacity_is_counted_once_across_tasks_comparison_and_evidence(ready,True)


def test_grouping_preserves_independently_declared_tasks(subset):
    from tests.test_nlp_task_subsets import publish

    fixture, _, profile, _, _, policy, _ = subset
    flat, request = publish(subset)
    bindings = profile_bindings(profile)
    declaration = CategoryNavigationDescriptor(policy_id='navigation.subset', revision='1',
        source_presentation=profile.presentation, bindings=bindings, groups=tuple(
            NavigationGroup(group_id=binding.task, display_label='Declared categories',
                            task=binding.task, member_ids=binding.enabled_ids)
            for binding in bindings))
    owner = QuestionResources(fixture.source, fixture.pipeline, clock=lambda:fixture.clock.now,
                              findings_navigation=NavigationRegistry((declaration,)))
    try:
        grouped = owner.grouped_findings(policy, GroupedFindingsRequest.model_validate(
            request.model_dump() | {'view':'grouped'}))
        assert grouped.tasks == flat.tasks and grouped.task_availability == flat.task_availability
        assert {row.task for row in grouped.navigation.current} == {binding.task for binding in bindings}
        mentions = next(row for row in grouped.navigation.current if row.task=='concept_mentions')
        assert mentions.supporting_messages == 2 and mentions.share_of_complete_messages == 1
        assert grouped.tasks[-1].rows[0].occurrences == 4
    finally:
        owner.close()
        assert owner.wait_closed()
