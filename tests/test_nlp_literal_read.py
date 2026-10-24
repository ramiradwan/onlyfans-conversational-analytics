"""Saved literal counts, exact evidence and authenticated read boundaries."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionBackpressure, ProjectionUnavailable
from app.analytics.nlp_literals import LiteralUnavailable
from app.analytics.opaque_refs import account_ref, conversation_ref
from app.analytics.query_execution import QuestionCancelled
from app.models.insights import FindingsCategoryFilter
from app.models.nlp_literals import LiteralFindingsRequest, LiteralFindingsResponse
from tests.continuous_analytics_fixture import ACCOUNT, advance
from tests.test_nlp_read_projection import api, ready  # noqa: F401

pytestmark = pytest.mark.ci_tier('integration')


def plan(ready, **overrides):
    return LiteralFindingsRequest(**{**ready.request.model_dump(), **overrides})


def test_repeated_literals_have_distinct_counts_partial_support_and_unicode_evidence(ready):
    response = ready.resources.literal_findings(ready.policy, plan(ready))
    assert response.schema_version == 'analytics-literals.v1'
    assert response.availability == 'available'
    assert response.normalization_coverage.model_dump() == {
        'eligible_occurrences': 5, 'mapped_occurrences': 5,
        'unrepresentable_occurrences': 0, 'supporting_messages': 3}
    assert response.extraction.eligible_messages == 8
    assert response.extraction.complete_messages == 3
    assert response.extraction.partial_messages == 1
    assert response.row_page.total == 1
    row = response.row_page.items[0]
    assert (row.literal_key, row.occurrences, row.supporting_messages, row.supporting_conversations) == ('echo', 5, 3, 1)
    assert (row.complete_supporting_messages, row.partial_supporting_messages) == (2, 1)
    assert response.evidence_page.total == 5
    assert len({(item.reference.message_ref, item.reference.span.start, item.reference.span.end)
                for item in response.evidence_page.items}) == 5
    item = next(item for item in response.evidence_page.items if item.reference.span.start == 7)
    resolved = ready.resources.resolve(ready.policy, item.reference)
    assert resolved.text[7:11] == 'echo'
    assert (resolved.browser_span.start, resolved.browser_span.end) == (8, 12)


def test_literal_pages_selection_and_pin_keep_population_counts(ready):
    first = ready.resources.literal_findings(ready.policy, plan(ready, evidence_limit=2))
    pinned = dict(expected_normalization_sha256=first.normalization_sha256, node_id=first.row_page.items[0].node_id)
    second = ready.resources.literal_findings(ready.policy, plan(ready, evidence_limit=2, evidence_offset=2, **pinned))
    assert first.row_page == second.row_page
    assert first.normalization_coverage == second.normalization_coverage
    assert first.extraction == second.extraction
    assert not {item.reference.model_dump_json() for item in first.evidence_page.items} & {
        item.reference.model_dump_json() for item in second.evidence_page.items}
    assert second.evidence_page.has_more
    with pytest.raises(ProjectionUnavailable):
        ready.resources.literal_findings(ready.policy, plan(ready, expected_normalization_sha256='sha256:' + 'f' * 64))
    with pytest.raises(InvalidAnalyticsRequest):
        ready.resources.literal_findings(ready.policy, plan(ready, **{**pinned, 'node_id': 'sha256:' + 'f' * 64}))
    with pytest.raises(InvalidAnalyticsRequest):
        ready.resources.literal_findings(ready.policy, plan(ready, row_offset=2, expected_normalization_sha256=first.normalization_sha256))
    for change in ({'evidence_offset': 1}, {'row_offset': 1}, {'node_id': first.row_page.items[0].node_id},
                   {'conversation_offset': 1}, {'conversation_limit': 1}):
        with pytest.raises(ValidationError):
            plan(ready, **change)


@pytest.mark.parametrize('ready', ['categories'], indirect=True)
def test_topic_and_literal_filters_match_the_same_message_without_changing_coverage(ready):
    full = ready.resources.literal_findings(ready.policy, plan(ready))
    filtered = ready.resources.literal_findings(ready.policy, plan(ready,
        category_filter=FindingsCategoryFilter(task='message_topics', category_id='topic.synthetic')))
    assert filtered.extraction == full.extraction
    assert filtered.normalization_coverage == full.normalization_coverage
    assert filtered.row_page.items[0].occurrences == 2
    assert filtered.row_page.items[0].supporting_messages == 1
    assert filtered.evidence_page.total == 2
    zero = ready.resources.literal_findings(ready.policy, plan(ready,
        category_filter=FindingsCategoryFilter(task='message_topics', category_id='topic.second')))
    assert zero.row_page.total == zero.evidence_page.total == 0
    assert zero.normalization_coverage == full.normalization_coverage
    assert zero.extraction.eligible_messages == 8
    with pytest.raises(InvalidAnalyticsRequest):
        ready.resources.literal_findings(ready.policy, plan(ready,
            category_filter=FindingsCategoryFilter(task='message_topics', category_id='topic.disabled')))


def test_concept_filter_keeps_other_types_from_matching_messages_only(ready):
    import json
    from app.nlp.profiles import AnalysisProfile
    from tests.test_nlp_read_projection import FixtureAdapter

    document = ready.profile.model_dump(mode='json')
    component = document['components'][0]
    component['tasks'][1]['enabled_ids'].append('concept.other')
    component['executed_labels'].append({
        'task': 'concept_mentions', 'label_id': 'concept.other',
        'description': 'Another synthetic concept type'})
    profile = AnalysisProfile.model_validate_json(json.dumps(document))

    class MixedTypes(FixtureAdapter):
        def __call__(self, request, cancelled):
            response = json.loads(super().__call__(request, cancelled))
            number = int(request.items[0].text.rsplit('case', 1)[1])
            payload = response['items'][0]['tasks'][1]['payload']
            for finding in payload['findings']:
                if number == 1 or finding['span']['start'] == 7:
                    finding['concept_id'] = 'concept.other'
            return json.dumps(response).encode()

    ready.runner.adapter = MixedTypes(profile)
    ready.runner.findings.select(ACCOUNT, profile)
    for _ in range(8):
        assert ready.runner.process_one(ACCOUNT)
    run = ready.runner.publish_pending(ACCOUNT)
    baseline = dict(expected_generation=run.artifact.projection.projection_generation,
                    expected_projection_digest=run.artifact.projection.projection_digest)
    full = ready.resources.literal_findings(ready.policy, plan(ready, **baseline))
    filtered = ready.resources.literal_findings(ready.policy, plan(ready, **baseline,
        category_filter=FindingsCategoryFilter(task='concept_mentions', category_id='concept.synthetic')))
    assert filtered.extraction == full.extraction
    assert filtered.normalization_coverage == full.normalization_coverage
    rows = {row.category_id: row for row in filtered.row_page.items}
    assert {key: row.occurrences for key, row in rows.items()} == {
        'concept.synthetic': 2, 'concept.other': 1}
    assert rows['concept.synthetic'].type_id != rows['concept.other'].type_id
    assert filtered.evidence_page.total == 3
    selected = ready.resources.literal_findings(ready.policy, plan(ready, **baseline,
        category_filter=FindingsCategoryFilter(task='concept_mentions', category_id='concept.synthetic'),
        node_id=rows['concept.other'].node_id, expected_normalization_sha256=filtered.normalization_sha256))
    assert selected.row_page == filtered.row_page
    assert selected.evidence_page.total == 1
    assert selected.evidence_page.items[0].reference.span.start == 7


def test_direction_conversation_and_exact_date_window_select_original_support(ready):
    inbound = ready.resources.literal_findings(ready.policy, plan(ready, direction='inbound'))
    outbound = ready.resources.literal_findings(ready.policy, plan(ready, direction='outbound'))
    assert (inbound.row_page.items[0].occurrences, inbound.row_page.items[0].supporting_messages) == (3, 2)
    assert (outbound.row_page.items[0].occurrences, outbound.row_page.items[0].supporting_messages) == (2, 1)
    empty = ready.resources.literal_findings(ready.policy, plan(ready, conversation_ref=conversation_ref(ACCOUNT, 'chat-1')))
    assert empty.extraction.eligible_messages == 4
    assert empty.row_page.total == empty.evidence_page.total == 0
    assert empty.normalization_coverage.eligible_occurrences == 0
    at = inbound.evidence_page.items[0].reference.sent_at
    exact = ready.resources.literal_findings(ready.policy, plan(ready, start_date=at, end_date=at))
    assert exact.extraction.eligible_messages == 1
    assert exact.effective_window.start == exact.effective_window.end == at
    outside = ready.resources.literal_findings(ready.policy, plan(ready,
        start_date=ready.fixture.clock.now - timedelta(days=1), end_date=ready.fixture.clock.now))
    assert outside.extraction.eligible_messages == 0
    assert outside.row_page.total == outside.evidence_page.total == 0


def test_expected_unavailability_retains_canonical_findings_and_cannot_replace_a_pin(ready, monkeypatch):
    from app.analytics import nlp_literals
    original = ready.resources.findings(ready.policy, ready.request)
    current = ready.resources.literal_findings(ready.policy, plan(ready))
    monkeypatch.setattr(nlp_literals, 'checked_publication', lambda *args, **kwargs: (_ for _ in ()).throw(LiteralUnavailable('capacity')))
    unavailable = ready.resources.literal_findings(ready.policy, plan(ready))
    assert unavailable.availability == 'unavailable' and unavailable.unavailable_reason == 'capacity'
    assert unavailable.extraction.complete_messages == 3
    assert unavailable.normalization_sha256 is None and unavailable.normalization_coverage is None
    assert unavailable.row_page.total == unavailable.evidence_page.total == 0
    assert ready.resources.findings(ready.policy, ready.request) == original
    with pytest.raises(ProjectionUnavailable):
        ready.resources.literal_findings(ready.policy, plan(ready, expected_normalization_sha256=current.normalization_sha256))


def test_publication_revocation_after_aggregation_is_rechecked_and_evidence_cleared(ready, monkeypatch):
    from app.analytics import nlp_literals
    real_assert = nlp_literals.assert_publication
    real_clear = ready.resources.evidence.clear_account
    cleared = []

    def clear(policy):
        cleared.append(policy)
        real_clear(policy)

    monkeypatch.setattr(ready.resources.evidence, 'clear_account', clear)

    def revoke(connection, binding, publication_id, **kwargs):
        with ready.fixture.stores.database.transaction() as db:
            db.execute('DELETE FROM nlp_literal_publications WHERE account_ref=?', (account_ref(ACCOUNT),))
        return real_assert(connection, binding, publication_id, **kwargs)

    monkeypatch.setattr(nlp_literals, 'assert_publication', revoke)
    with pytest.raises(LiteralUnavailable):
        ready.resources.literal_findings(ready.policy, plan(ready))
    assert cleared == [ready.policy]
    assert ready.resources.findings(ready.policy, ready.request).tasks[1].rows[0].occurrences == 5


def test_batch_revocation_with_unchanged_manifest_is_rejected_by_content_fence(ready, monkeypatch):
    from app.analytics import nlp_literals
    real_assert = nlp_literals.assert_publication

    def revoke_batch(connection, binding, publication_id, **kwargs):
        with ready.fixture.stores.database.transaction() as db:
            db.execute('DELETE FROM nlp_literal_publication_batches WHERE account_ref=? AND ordinal=0',
                       (account_ref(ACCOUNT),))
        return real_assert(connection, binding, publication_id, **kwargs)

    monkeypatch.setattr(nlp_literals, 'assert_publication', revoke_batch)
    with pytest.raises(ProjectionUnavailable) as failure:
        ready.resources.literal_findings(ready.policy, plan(ready))
    assert failure.value.availability == 'building'


@pytest.mark.parametrize('literal', [False, True])
def test_reads_crossing_retention_expiry_are_rejected_before_return(ready, monkeypatch, literal):
    from app.analytics import nlp_literal_read, nlp_read_projection
    module = nlp_literal_read if literal else nlp_read_projection
    original = module.aggregate

    def crossing(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(ready.resources, 'clock', lambda: ready.fixture.clock.now + timedelta(days=1000))
        return result

    monkeypatch.setattr(module, 'aggregate', crossing)
    with pytest.raises(ProjectionUnavailable) as failure:
        if literal:
            ready.resources.literal_findings(ready.policy, plan(ready))
        else:
            ready.resources.findings(ready.policy, ready.request)
    assert failure.value.availability == 'building'


def test_shared_cancellation_slots_and_source_edit_refuse_stale_literal_reads(ready):
    with pytest.raises(QuestionCancelled):
        ready.resources.literal_findings(ready.policy, plan(ready), cancellation_check=lambda: True)
    assert ready.resources._slots.acquire(blocking=False)
    assert ready.resources._slots.acquire(blocking=False)
    try:
        with pytest.raises(ProjectionBackpressure):
            ready.resources.literal_findings(ready.policy, plan(ready))
    finally:
        ready.resources._slots.release()
        ready.resources._slots.release()
    with ready.fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='edited' WHERE message_id='m-0-0'")
        advance(db)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.literal_findings(ready.policy, plan(ready))


def test_http_literal_reads_are_closed_no_store_pinned_and_do_not_schedule_analysis(api, ready):
    client, scheduler = api
    query = {'expected_generation': ready.request.expected_generation,
             'expected_projection_digest': ready.request.expected_projection_digest}
    path = '/api/v1/insights/findings/literals'
    response = client.get(path, params=query)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['row_page']['items'][0]['literal_key'] == 'echo'
    reference = next(item['reference'] for item in body['evidence_page']['items'] if item['reference']['span']['start'] == 7)
    resolved = client.post('/api/v1/insights/questions/evidence', json=reference)
    assert resolved.status_code == 200 and resolved.json()['text'][7:11] == 'echo'
    for extra in ({'account_id': 'other'}, {'category_task': 'message_topics'},
                  {'row_limit': 101}, {'row_offset': 1}, {'node_id': 'bad'},
                  {'conversation_offset': 1}, {'direction': 'sideways'}):
        assert client.get(path, params={**query, **extra}).status_code == 422
    duplicate = list(query.items()) + [('direction', 'all'), ('direction', 'all')]
    assert client.get(path, params=duplicate).status_code == 422
    changed = client.get(path, params={**query, 'expected_normalization_sha256': 'sha256:' + 'f' * 64})
    assert changed.status_code == 503 and changed.headers['cache-control'] == 'no-store'
    scheduler.request_recovery.assert_not_called()


def test_response_contract_requires_full_occurrence_accounting(ready):
    value = ready.resources.literal_findings(ready.policy, plan(ready)).model_dump(mode='json')
    for field, changed in (('mapped_occurrences', 0), ('unrepresentable_occurrences', 1)):
        broken = {**value, 'normalization_coverage': {**value['normalization_coverage'], field: changed}}
        with pytest.raises(ValidationError):
            LiteralFindingsResponse.model_validate(broken)
    broken = {**value, 'availability': 'unavailable', 'unavailable_reason': 'not_prepared'}
    with pytest.raises(ValidationError):
        LiteralFindingsResponse.model_validate(broken)
