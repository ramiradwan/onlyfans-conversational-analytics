"""Enrollment HTTP commands through durable sessions and CSRF validation."""
from uuid import uuid4

import pytest

from app.persistence.auth import RevocationKey, RevocationScopeType
from app.services.text_analysis_execution import configure_execution_service
from tests.text_analysis_execution_fixture import (
    make_execution_fixture, close_execution_fixture, command, execution_http_client,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/execution'


@pytest.fixture
def http(tmp_path):
    x = make_execution_fixture(tmp_path)
    try:
        with execution_http_client(x) as client:
            yield x, client
    finally:
        close_execution_fixture(x)


def test_actual_session_command_read_and_unknown_outcome_recovery(http):
    x, client = http
    before = (x.environment.engine.prepares, list(x.environment.requests), x.source.full_reads)
    assert client.get(PATH).json()['mode_version'] == 0
    cmd = command(x)
    response = client.post(PATH, json=cmd.model_dump(mode='json'))
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['current']['enrollment_id']
    assert client.get(PATH+'/commands/'+cmd.command_key).json() == first
    assert client.get(PATH+'/commands/'+str(uuid4())).status_code == 404
    x.environment.engine.present = False
    retry = client.post(PATH, json=cmd.model_dump(mode='json'))
    assert retry.status_code == 200 and retry.json() == first
    assert before == (x.environment.engine.prepares, x.environment.requests, x.source.full_reads)
    assert str(x.auth.database.path) not in response.text


@pytest.mark.parametrize('fault', ['no-session', 'origin', 'host', 'csrf', 'foreign-field', 'numeric'])
def test_strict_http_boundary_refuses_without_canonical_write(http, fault):
    x, client = http
    body = command(x).model_dump(mode='json')
    if fault == 'no-session':
        client.cookies.clear()
    elif fault == 'origin':
        client.headers['Origin'] = 'http://foreign.invalid'
    elif fault == 'host':
        client.headers['Host'] = 'foreign.invalid'
    elif fault == 'csrf':
        client.headers['X-CSRF-Token'] = 'a'*43
    elif fault == 'numeric':
        body['expected_mode_version'] = 0.0
    else:
        body['run_context'] = 'caller-owned'
    response = client.post(PATH, json=body)
    assert response.status_code == (401 if fault == 'no-session' else 422 if fault in ('numeric','foreign-field') else 403)
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollment_commands').fetchone()[0] == 0


def test_logout_refuses_real_http_policy(http):
    x, client = http
    cmd = command(x)
    x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, x.http_policy().identity.session_id))
    assert client.post(PATH, json=cmd.model_dump(mode='json')).status_code == 401
    assert client.get(PATH).status_code == 401


def test_identity_grant_withdrawal_refuses_real_http_policy(http):
    x, client = http
    cmd = command(x)
    x.auth.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, x.identity.identity.grant_reference_ids[0]))
    assert client.post(PATH, json=cmd.model_dump(mode='json')).status_code == 401
    assert client.get(PATH).status_code == 401


@pytest.mark.parametrize('action', ['select', 'rollback', 'account_enable', 'account_pause', 'remove'])
def test_legacy_http_account_selection_is_fenced_by_canonical_owner(http, action):
    x, client = http
    x.service.command(x.http_policy(), command(x))
    before = x.dependencies.findings.state(x.context.creator_account_id)
    body = {'action': action}
    if action == 'select':
        body.update(profile_id=x.profile.profile_id, revision=x.profile.revision)
    response = client.post('/api/v1/nlp/dependencies', json=body)
    assert response.status_code == 409
    assert x.dependencies.findings.state(x.context.creator_account_id) == before


def test_absent_composition_refuses_without_starting_work(http):
    x, client = http
    configure_execution_service(None)
    assert client.get(PATH).status_code == 503
    assert client.post(PATH, json=command(x).model_dump(mode='json')).status_code == 503
