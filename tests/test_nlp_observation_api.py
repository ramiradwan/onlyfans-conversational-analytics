"""Observation focus parsing and authenticated source retrieval."""
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from app.analytics.errors import ProjectionUnavailable
from app.analytics.validation_receipt import content_stamp
from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session
from app.api.endpoints import insights
from app.security.local_sessions import build_runtime_policy
from app.security.runtime_policy import AuthContext
from app.services import insights_service
from tests.test_nlp_read_projection import api, ready  # noqa: F401

pytestmark = pytest.mark.ci_tier('integration')
PATH = '/api/v1/insights/findings/observations'
DIGEST = 'sha256:' + '1' * 64


def policy():
    return build_runtime_policy(AuthContext('principal.synthetic', 'account.synthetic', 'creator',
        platform_creator_id='platform.synthetic', session_id='session.synthetic'))


@pytest.fixture
def closed_api(monkeypatch):
    dispatcher = AsyncMock()
    monkeypatch.setattr(insights_service, 'fetch_observation_links', dispatcher)
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[require_activated_runtime] = lambda: None
    app.dependency_overrides[get_authenticated_account_session] = policy
    with TestClient(app) as client:
        yield client, dispatcher


def test_observation_query_rejects_unknown_duplicate_and_ambiguous_focus(closed_api):
    client, dispatcher = closed_api
    base = {'expected_generation': 1, 'expected_projection_digest': DIGEST,
            'category_task': 'message_topics', 'category_id': 'topic.synthetic'}
    invalid = [
        {'node_id': DIGEST}, {'category_task': 'unknown'}, {'category_id': ''},
        {'focus': '{}'}, {'creator_account_id': 'other'}, {'row_offset': 0},
        {'conversation_offset': 0}, {'view': 'grouped'}, {'evidence_limit': 101},
        {'evidence_offset': -1}, {'start_date': '2026-10-01'},
        {'start_date': '2026-10-02T00:00:00Z', 'end_date': '2026-10-01T00:00:00Z'},
        {'expected_normalization_sha256': DIGEST}, {'evidence_offset': 1},
        {'expected_generation': 'not-a-number'}, {'expected_projection_digest': 'invalid-value'},
        {'conversation_ref': 'invalid-value'}, {'direction': 'invalid-value'},
        {'expected_view_sha256': 'invalid-value'},
    ]

    def rejected(params, *, code='analytics_findings_request_invalid', message='The findings request is invalid.'):
        response = client.get(PATH, params=params)
        assert response.status_code == 422
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {'detail': {'code': code, 'message': message, 'availability': 'unavailable'}}

    for extra in invalid:
        if extra.get('start_date') == '2026-10-01':
            rejected(base | extra, code='analytics_timestamp_timezone_required',
                     message='Analytics timestamps must include a timezone.')
        else:
            rejected(base | extra)
    for omitted in (('category_id',), ('category_task',), ('category_task', 'category_id'),
                    ('expected_generation',), ('expected_projection_digest',)):
        rejected({k: v for k, v in base.items() if k not in omitted})
    for key in base:
        rejected(list(base.items()) + [(key, base[key])])
    literal = {'expected_generation': 1, 'expected_projection_digest': DIGEST, 'node_id': DIGEST}
    rejected(literal)
    dispatcher.assert_not_called()


def test_observation_validation_wrapper_preserves_typed_openapi_and_other_routes(closed_api):
    client, _ = closed_api
    routes = [route for route in insights.router.routes if isinstance(route, insights._ObservationReadRoute)]
    assert len(routes) == 1 and routes[0].path == PATH
    operation = client.app.openapi()['paths'][PATH]['get']
    assert operation['operationId'] == 'fetchObservationLinks'
    parameters = {item['name']: item for item in operation['parameters']}
    assert parameters['expected_generation']['required']
    assert parameters['expected_projection_digest']['required']
    assert parameters['evidence_limit']['schema']['type'] == 'integer'
    assert parameters['evidence_limit']['schema']['maximum'] == 100
    assert operation['responses']['422']['content']['application/json']['schema'] == {
        '$ref': '#/components/schemas/AnalyticsErrorResponse'}


@pytest.mark.asyncio
@pytest.mark.parametrize('focus', [
    {'category_task': 'message_topics', 'category_id': 'topic.synthetic'},
    {'category_task': 'concept_mentions', 'category_id': 'concept.synthetic'},
    {'node_id': DIGEST, 'expected_normalization_sha256': DIGEST},
])
async def test_observation_parser_constructs_exact_focus_without_unused_fields(monkeypatch, focus):
    actual_policy, result = policy(), object()
    dispatcher = AsyncMock(return_value=result)
    monkeypatch.setattr(insights_service, 'fetch_observation_links', dispatcher)
    parameters = dict(expected_generation=1, expected_projection_digest=DIGEST,
        start_date=None, end_date=None, direction='all', evidence_offset=0, evidence_limit=50,
        conversation_ref=None, category_task=None, category_id=None, node_id=None,
        expected_normalization_sha256=None, expected_view_sha256=None) | focus
    query = {k: v for k, v in parameters.items() if v is not None}
    request = Request({'type': 'http', 'query_string': urlencode(query).encode(), 'headers': []})
    response = Response()
    assert await insights.get_observation_links(request, response, policy=actual_policy, **parameters) is result
    assert response.headers['cache-control'] == 'no-store'
    actual, plan = dispatcher.call_args.args
    assert actual is actual_policy
    assert plan.focus.kind == ('literal' if 'node_id' in focus else 'category')
    if 'node_id' in focus:
        assert plan.focus.node_id == focus['node_id']
    else:
        assert plan.focus.selection.task == focus['category_task']
        assert plan.focus.selection.category_id == focus['category_id']
    assert not {'row_offset', 'row_limit', 'conversation_offset', 'conversation_limit'} & set(type(plan).model_fields)


def test_observation_http_uses_existing_error_contract(closed_api):
    client, dispatcher = closed_api
    dispatcher.side_effect = ProjectionUnavailable(building=True)
    response = client.get(PATH, params={'expected_generation': 1, 'expected_projection_digest': DIGEST,
        'category_task': 'message_topics', 'category_id': 'topic.synthetic'})
    assert response.status_code == 503 and response.headers['cache-control'] == 'no-store'
    assert response.json()['detail']['code'] == 'analytics_projection_building'


def test_observation_http_reads_topic_and_literal_sources_without_writes_or_recovery(api, ready):
    client, scheduler = api
    base = {'expected_generation': ready.request.expected_generation,
            'expected_projection_digest': ready.request.expected_projection_digest}
    with ready.fixture.stores.database.read() as db:
        before = content_stamp(db)
    topic = client.get(PATH, params=base | {'category_task': 'message_topics', 'category_id': 'topic.synthetic'})
    assert topic.status_code == 200, topic.text
    assert topic.headers['cache-control'] == 'no-store'
    topic_body = topic.json()
    assert topic_body['schema_version'] == 'analytics-observation-links.v1'
    assert topic_body['normalization_status'] == 'not_required'
    assert topic_body['link_page']['total'] == 3
    assert all(item['kind'] == 'message_topic' and item['reference']['span'] is None
               for item in topic_body['link_page']['items'])
    mention = client.get(PATH, params=base | {'category_task': 'concept_mentions', 'category_id': 'concept.synthetic'})
    assert mention.status_code == 200, mention.text
    body = mention.json()
    assert body['normalization_status'] == 'available' and body['link_page']['total'] == 5
    reference = next(item['reference'] for item in body['link_page']['items']
                     if item['reference']['span']['start'] == 7)
    source = client.post('/api/v1/insights/questions/evidence', json=reference)
    assert source.status_code == 200 and source.json()['text'][7:11] == 'echo'
    node = body['link_page']['items'][0]['target']['node_id']
    literal = client.get(PATH, params=base | {'node_id': node,
        'expected_normalization_sha256': body['normalization_sha256']})
    assert literal.status_code == 200, literal.text
    assert literal.json()['link_page']['total'] == 5
    later = client.get(PATH, params=base | {'category_task': 'concept_mentions', 'category_id': 'concept.synthetic',
        'evidence_limit': 1, 'evidence_offset': 1, 'expected_view_sha256': body['view_sha256'],
        'expected_normalization_sha256': body['normalization_sha256']})
    assert later.status_code == 200, later.text
    assert later.json()['link_page']['items'] == body['link_page']['items'][1:2]
    stale = client.get(PATH, params=base | {'category_task': 'message_topics', 'category_id': 'topic.synthetic',
        'expected_view_sha256': DIGEST})
    assert stale.status_code == 503 and stale.headers['cache-control'] == 'no-store'
    with ready.fixture.stores.database.read() as db:
        assert content_stamp(db) == before
    scheduler.request_recovery.assert_not_called()
