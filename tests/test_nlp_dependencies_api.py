"""Session-bound dependency commands and side-effect-free cached observations."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session
from app.api.endpoints import nlp_dependencies as endpoint
from app.api import security
from app.security.local_sessions import build_runtime_policy, issue_csrf_token
from app.security.runtime_policy import AuthContext
from tests.test_nlp_dependencies import env
from tests.continuous_analytics_fixture import ACCOUNT

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture
def client(env, monkeypatch):
    policy = build_runtime_policy(AuthContext('principal.synthetic', ACCOUNT, 'creator',
        platform_creator_id='platform.synthetic', session_id='session.synthetic'))
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[require_activated_runtime] = lambda: None
    app.dependency_overrides[get_authenticated_account_session] = lambda: policy
    monkeypatch.setattr(endpoint, 'dependency_service', lambda: env.service)
    monkeypatch.setattr(security, '_development_context_allowed', lambda: False)
    monkeypatch.setattr(security.settings, 'bridge_origin', 'http://testserver')
    with TestClient(app) as http:
        http.headers.update({'Origin': 'http://testserver', 'X-CSRF-Token': issue_csrf_token(policy)})
        yield http


def test_read_lists_catalog_and_bound_selection_without_network_or_warmup(client, env):
    before = env.persistence.authority()
    calls = list(env.requests)
    response = client.get('/api/v1/nlp/dependencies')
    assert response.status_code == 200
    assert response.json()['profiles'][0]['profile_id'] == env.profile.profile_id
    assert response.json()['selected'] is None
    assert env.requests == calls and env.engine.prepares == 0
    assert env.persistence.authority() == before
    assert 'profile_json' not in response.text and 'directory' not in response.text


def test_plan_confirm_acquire_select_and_distinct_account_pause(client, env):
    path = '/api/v1/nlp/dependencies'
    target = {'profile_id': env.profile.profile_id, 'revision': env.profile.revision}
    quote = client.post(path, json={'action': 'plan', **target})
    assert quote.status_code == 200
    operation = quote.json()['operation']['operation_id']
    acquired = client.post(path, json={'action': 'acquire', 'operation_id': operation, **target})
    assert acquired.status_code == 200
    env.service.thread.join(10)
    cached = client.get(path).json()['acquisitions']
    assert len(cached) == 1 and cached[0]['profile_sha256'] == env.profile.identity_digest()
    assert cached[0]['state'] == 'complete' and not cached[0]['in_flight']
    assert client.post(path, json={'action': 'select', **target}).status_code == 200
    paused = client.post(path, json={'action': 'account_pause'})
    assert paused.status_code == 200 and paused.json()['status']['selected']['mode'] == 'paused'
    enabled = client.post(path, json={'action': 'account_enable'})
    assert enabled.status_code == 200 and enabled.json()['status']['selected']['mode'] == 'enabled'
    assert 'resources' in enabled.json()['status']['profiles'][0]


@pytest.mark.parametrize('fault', ['origin', 'csrf', 'account'])
def test_commands_require_origin_csrf_and_session_scope(client, env, fault):
    body = {'action': 'refresh'}
    if fault == 'origin':
        client.headers['Origin'] = 'http://other.invalid'
    elif fault == 'csrf':
        del client.headers['X-CSRF-Token']
    else:
        body['account_id'] = 'another.synthetic'
    before = list(env.requests)
    response = client.post('/api/v1/nlp/dependencies', json=body)
    assert response.status_code == (422 if fault == 'account' else 403)
    assert env.requests == before


def test_default_service_absence_has_no_authority_or_mutation(client, monkeypatch):
    monkeypatch.setattr(endpoint, 'dependency_service', lambda: None)
    read = client.get('/api/v1/nlp/dependencies')
    assert read.status_code == 200 and read.json()['profiles'] == []
    assert read.json()['selected'] is None
    assert client.post('/api/v1/nlp/dependencies', json={'action': 'refresh'}).status_code == 503


def test_invalid_operation_id_and_failed_command_do_not_disclose(client, env):
    assert client.get('/api/v1/nlp/dependencies?operation_id=invalid').status_code == 422
    response = client.post('/api/v1/nlp/dependencies', json={'action': 'prepare', 'profile_sha256': 'f'*64})
    assert response.status_code == 409
    assert response.json()['detail'] == {'code': 'nlp_dependency_refused'}
    assert str(env.root) not in json.dumps(response.json())
