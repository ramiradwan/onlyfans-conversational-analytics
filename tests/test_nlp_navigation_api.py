"""Grouped HTTP reads preserve saved state and existing evidence authority."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.analytics.errors import ProjectionBackpressure, ProjectionUnavailable
from app.analytics.nlp_navigation import NavigationRegistry
from app.analytics.query_runtime import QuestionResources
from app.analytics.runtime import AnalyticsRuntime
from app.analytics.validation_receipt import content_stamp
from app.models.insights import GroupedFindingsRequest
from app.nlp.navigation import navigation_identity
from tests.test_nlp_navigation_read import descriptor
from tests.test_nlp_read_projection import ready

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture
def navigation_api(ready, monkeypatch):
    from app.api.activation import require_activated_runtime
    from app.api.dependencies import get_authenticated_account_session
    from app.api.endpoints import insights
    from app.api.security import csrf_token
    from app.services import insights_service

    declaration = descriptor(ready.profile)
    owner = QuestionResources(ready.fixture.source, ready.fixture.pipeline,
        clock=lambda: ready.fixture.clock.now,
        findings_navigation=NavigationRegistry((declaration,)))
    scheduler = SimpleNamespace(request_recovery=AsyncMock())
    runtime = AnalyticsRuntime(ready.fixture.source, ready.fixture.pipeline, scheduler, owner)
    monkeypatch.setattr(insights_service, 'analytics_runtime', lambda: runtime)
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[get_authenticated_account_session] = lambda: ready.policy
    app.dependency_overrides[require_activated_runtime] = lambda: None
    try:
        with TestClient(app) as client:
            client.headers['x-csrf-token'] = csrf_token(ready.policy)
            yield SimpleNamespace(client=client, owner=owner, scheduler=scheduler,
                                  declaration=declaration)
    finally:
        owner.close()
        assert owner.wait_closed()


def query(ready):
    return {'expected_generation': ready.request.expected_generation,
            'expected_projection_digest': ready.request.expected_projection_digest}


def test_grouped_http_reuses_frozen_findings_and_exact_source_evidence(navigation_api, ready):
    api = navigation_api
    path = '/api/v1/insights/findings'
    with ready.fixture.stores.database.read() as db:
        before = content_stamp(db)
        assert before is not None
    flat = api.client.get(path, params=query(ready))
    grouped = api.client.get(path, params=query(ready) | {'view': 'grouped'})
    assert flat.status_code == grouped.status_code == 200
    body = grouped.json()
    assert grouped.headers['cache-control'] == 'no-store'
    assert body['schema_version'] == 'analytics-findings.v4'
    assert body['category_filter'] is None
    assert body['navigation']['policy'] == navigation_identity(api.declaration).model_dump(mode='json')
    for field in ('profile_sha256', 'presentation', 'snapshot_sha256', 'tasks', 'comparison',
                  'conversation_page', 'evidence_page'):
        assert body[field] == flat.json()[field]
    reference = next(item['reference'] for item in body['evidence_page']['items']
                     if item['reference']['span'] and item['reference']['span']['start'] == 7)
    source = api.client.post('/api/v1/insights/questions/evidence', json=reference)
    assert source.status_code == 200 and source.json()['text'][7:11] == 'echo'
    with ready.fixture.stores.database.read() as db:
        assert content_stamp(db) == before
    api.scheduler.request_recovery.assert_not_called()


def test_grouped_http_is_closed_and_pins_pages_without_scheduling(navigation_api, ready):
    api = navigation_api
    path = '/api/v1/insights/findings'
    base = query(ready) | {'view': 'grouped'}
    pin = 'sha256:' + navigation_identity(api.declaration).sha256
    first = api.client.get(path, params=base)
    assert first.status_code == 200
    page = api.client.get(path, params=base | {'evidence_offset': 1, 'evidence_limit': 1,
                                             'expected_navigation_sha256': pin})
    assert page.status_code == 200, page.text
    assert page.json()['evidence_page']['items'] == first.json()['evidence_page']['items'][1:2]
    for extra in ({'account_id': 'other'}, {'category_task': 'message_topics'},
                  {'evidence_offset': 1}, {'conversation_offset': 1},
                  {'view': 'flat'}, {'navigation': '{}'}, {'expected_navigation_sha256': 'bad'}):
        assert api.client.get(path, params=base | extra).status_code == 422
    duplicate = list(base.items()) + [('view', 'grouped')]
    assert api.client.get(path, params=duplicate).status_code == 422
    flat_pin = api.client.get(path, params=query(ready) | {'expected_navigation_sha256': pin})
    assert flat_pin.status_code == 422 and flat_pin.headers['cache-control'] == 'no-store'
    for change in ({'expected_navigation_sha256': 'sha256:' + 'f' * 64},
                   {'expected_generation': 1000}):
        stale = api.client.get(path, params=base | change)
        assert stale.status_code == 503 and stale.headers['cache-control'] == 'no-store'
    api.scheduler.request_recovery.assert_not_called()


def test_undeclared_grouping_keeps_flat_reads_and_shares_existing_limits(ready):
    owner = ready.resources
    plan = GroupedFindingsRequest.model_validate(ready.request.model_dump() | {'view': 'grouped'})
    result = owner.grouped_findings(ready.policy, plan)
    assert result.navigation.availability == 'not_declared'
    assert result.navigation.policy is None and result.navigation.current == ()
    assert result.tasks == owner.findings(ready.policy, ready.request).tasks
    assert owner._slots.acquire(blocking=False)
    assert owner._slots.acquire(blocking=False)
    try:
        with pytest.raises(ProjectionBackpressure):
            owner.grouped_findings(ready.policy, plan)
    finally:
        owner._slots.release()
        owner._slots.release()
    with pytest.raises(AttributeError):
        owner.findings_navigation = NavigationRegistry()
    owner.close()
    assert owner.wait_closed()
    with pytest.raises(ProjectionUnavailable):
        owner.grouped_findings(ready.policy, plan)
