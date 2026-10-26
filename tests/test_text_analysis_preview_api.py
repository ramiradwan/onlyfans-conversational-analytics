"""Actual durable-session preview routes, closed bodies and noncreating reads."""
from uuid import uuid4

import pytest

from tests.text_analysis_preview_fixture import (
    make_preview_fixture, close_preview_fixture, preview_http_client, request,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/previews'


@pytest.fixture
def preview(tmp_path):
    x = make_preview_fixture(tmp_path)
    try:
        yield x
    finally:
        close_preview_fixture(x)


def test_actual_session_prepares_confirms_and_recovers_same_immutable_request(preview):
    x = preview
    body = request(x)
    with preview_http_client(x) as client:
        prepared = client.post(PATH, json=body.model_dump(mode='json'))
        assert prepared.status_code == 200, prepared.text
        value = prepared.json()
        assert value['scope']['source_count'] == 2 and value['disclosure']['profile_sha256'] == x.profile.identity_digest()
        assert {task['task'] for task in value['disclosure']['tasks']} == {'message_topics', 'concept_mentions'}
        assert 'confirmation' not in value['receipt']
        assert client.post(PATH, json=body.model_dump(mode='json')).json() == value
        accepted = client.post(PATH+'/'+body.external_key+'/confirm', json=value['confirmation'])
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['receipt']['state'] == 'pending'
        assert client.get(PATH+'/'+body.external_key).json() == accepted.json()
        assert client.post(PATH+'/'+body.external_key+'/confirm', json=value['confirmation']).json() == accepted.json()
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 1
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'reserved'


def test_unknown_outcome_and_closed_body_never_allocate_work(preview):
    x = preview
    body = request(x)
    with preview_http_client(x) as client:
        assert client.get(PATH+'/'+str(uuid4())).status_code == 404
        assert client.post(PATH, json={**body.model_dump(mode='json'), 'text': 'unaccepted'}).status_code == 422
        assert client.post(PATH, json={**body.model_dump(mode='json'), 'external_key': 'not-a-uuid'}).status_code == 422
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
        assert not db.execute('SELECT 1 FROM text_analysis_preview_requests').fetchone()


def test_actual_csrf_and_origin_required_without_authorization_override(preview):
    x = preview
    with preview_http_client(x) as client:
        body = request(x).model_dump(mode='json')
        assert client.post(PATH, json=body, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
        del client.headers['X-CSRF-Token']
        assert client.post(PATH, json=body).status_code == 403
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
