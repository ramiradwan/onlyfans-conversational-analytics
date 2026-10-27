"""Real local sessions, durable reissue adoption and bounded browser handoff."""
from datetime import datetime, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.endpoints import text_analysis_reissue
from app.api.security import webauthn_session_token
from app.core.config import settings
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.security.activation_gate import ActivationDecision, record_activation, reset_activation
from app.services.text_analysis_reissue import configure_reissue_service, reissue_service
from app.services.text_analysis_reissue_navigation import ReissueNavigationEntries
from tests.test_text_analysis_authority import local_signer  # noqa: F401
from tests.text_analysis_reissue_fixture import make_reissue_fixture

pytestmark = pytest.mark.ci_tier('integration')
ORIGIN = 'http://bridge.localhost:17871'
PATH = '/api/v1/nlp/license-reissue'


@pytest.fixture
def http(tmp_path, monkeypatch, local_signer):
    x = make_reissue_fixture(tmp_path, local_signer, now=datetime.now(timezone.utc))
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    monkeypatch.setattr(settings, 'websocket_auth_mode', 'local_session')
    monkeypatch.setattr(settings, 'bridge_origin', ORIGIN)
    monkeypatch.setattr(text_analysis_reissue, '_entries', ReissueNavigationEntries())
    previous = reissue_service()
    configure_reissue_service(x.service)
    reset_activation()
    app = FastAPI()
    app.include_router(text_analysis_reissue.router)
    try:
        assert not app.dependency_overrides
        with TestClient(app, base_url=ORIGIN) as client:
            client.cookies.set(settings.bridge_session_cookie_name,
                webauthn_session_token(x.http_policy().identity, x.identity.session.session_value))
            client.headers.update({'Origin': ORIGIN, 'X-CSRF-Token': x.identity.session.csrf_value})
            yield x, client
    finally:
        configure_reissue_service(previous)
        reset_activation()
        x.client.transport.close()


def _count(x):
    with x.auth.database.read() as db:
        return db.execute('SELECT count(*) FROM text_analysis_reissue_operations').fetchone()[0]


def test_inert_status_and_explicit_actual_client_adoption_without_module_authority(http):
    x, client = http
    assert x.http_policy().text_analysis_authority is None
    ensures = x.client.key_authority.ensure_calls
    assert client.get(PATH).json() == {'current': None}
    assert client.get(PATH).json() == {'current': None}
    assert _count(x) == 0 and x.requests == []
    assert x.client.key_authority.ensure_calls == ensures
    first = client.post(PATH, json={})
    assert first.status_code == 200, first.text
    current = first.json()['current']
    assert set(current) == {'flow_id', 'state', 'navigation_available'}
    assert current['state'] == 'awaiting_authorization' and current['navigation_available']
    assert len(x.requests) == 2 and _count(x) == 1
    assert client.get(PATH).json() == first.json()
    assert len(x.requests) == 2
    finished = client.post(PATH + '/' + current['flow_id'] + '/resume', json={})
    assert finished.status_code == 200, finished.text
    assert finished.json()['current'] == dict(current, state='adopted', navigation_available=False)
    with x.auth.database.read() as db:
        row = db.execute('SELECT result,reference_id FROM text_analysis_reissue_operations').fetchone()
        assert bytes(row['result']).startswith(b' ') and bytes(row['result']).endswith(b'\n')
        assert json.loads(row['result'])['signed_authority']
        assert row['reference_id']
    requests = len(x.requests)
    assert client.post(PATH + '/' + current['flow_id'] + '/resume', json={}).json() == finished.json()
    assert len(x.requests) == requests
    assert all(used for _, used in x.challenges.values())
    assert 'signed_authority' not in finished.text and 'seat_id' not in finished.text
    assert finished.headers['cache-control'] == 'no-store'


def test_browser_entry_requires_activation_without_consuming_its_ticket(http, monkeypatch):
    x, client = http
    monkeypatch.setattr(text_analysis_reissue, '_entries', ReissueNavigationEntries(clock=lambda: 1.0))
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    code = client.post(PATH + '/' + flow + '/browser-entry', json={}).json()['entry_code']
    before = len(x.requests)
    record_activation(ActivationDecision(activated=False, production=True))

    with TestClient(client.app, base_url=ORIGIN) as browser:
        for path in ('/text-analysis-reissue/entry', '/text-analysis-reissue/entry.js'):
            assert browser.get(path).status_code == 503
        refused = browser.post(PATH + '/browser-entry/consume', json={'code': code}, headers={'Origin': ORIGIN})
        assert refused.status_code == 503
        assert len(x.requests) == before and _count(x) == 1

        record_activation(ActivationDecision(activated=True, production=True))
        accepted = browser.post(PATH + '/browser-entry/consume', json={'code': code}, headers={'Origin': ORIGIN})
        assert accepted.status_code == 200
        assert accepted.json() == {'url': 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43}
        assert not accepted.cookies and len(x.requests) == before


def test_one_use_browser_entry_has_no_authority_or_hidden_coordinates(http):
    x, client = http
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    entry = client.post(PATH + '/' + flow + '/browser-entry', json={})
    assert entry.status_code == 200, entry.text
    assert set(entry.json()) == {'entry_code'}
    assert len(entry.json()['entry_code']) == 43
    before = len(x.requests)
    with TestClient(client.app, base_url=ORIGIN) as browser:
        landing = browser.get('/text-analysis-reissue/entry')
        assert landing.status_code == 200 and 'entry_code' not in landing.text
        assert "frame-ancestors 'none'" in landing.headers['content-security-policy']
        opened = browser.post(PATH + '/browser-entry/consume', json={'code': entry.json()['entry_code']}, headers={'Origin': ORIGIN})
        assert opened.status_code == 200, opened.text
        assert opened.json() == {'url': 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43}
        assert not opened.cookies
        assert browser.post(PATH + '/browser-entry/consume', json={'code': entry.json()['entry_code']}, headers={'Origin': ORIGIN}).status_code == 401
    assert len(x.requests) == before
    assert client.get(PATH).json()['current']['state'] == 'awaiting_authorization'


@pytest.mark.parametrize('fault', ['session', 'origin', 'host', 'csrf', 'extra', 'duplicate', 'encoding', 'large', 'query'])
def test_mutation_refuses_before_new_intent_or_hosted_request(http, fault):
    x, client = http
    headers = {'Content-Type': 'application/json'}
    raw, path = b'{}', PATH
    if fault == 'session':
        client.cookies.clear()
    if fault == 'origin':
        headers['Origin'] = 'https://foreign.invalid'
    if fault == 'host':
        headers['Host'] = 'foreign.invalid'
    if fault == 'csrf':
        headers['X-CSRF-Token'] = 'invalid'
    if fault == 'extra':
        raw = b'{"release_identity":{}}'
    if fault == 'duplicate':
        raw = b'{"x":1,"x":2}'
    if fault == 'encoding':
        headers['Content-Encoding'] = 'gzip'
    if fault == 'large':
        raw = b' '*257
    if fault == 'query':
        path += '?scope=untrusted'
    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == (401 if fault == 'session' else 403 if fault in ('origin','host','csrf')
        else 415 if fault in ('encoding','query') else 413 if fault == 'large' else 422)
    assert _count(x) == 0 and x.requests == []


def test_revoked_local_session_cannot_discover_or_resume_reissue(http):
    x, client = http
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, x.http_policy().identity.session_id))
    before = len(x.requests)
    assert client.get(PATH).status_code == 401
    assert client.post(PATH + '/' + flow + '/resume', json={}).status_code == 401
    assert len(x.requests) == before and _count(x) == 1


def test_unconfigured_provider_refuses_without_creating_key_intent_or_request(http):
    x, client = http
    configure_reissue_service(None)
    before = x.client.key_authority.ensure_calls
    assert client.get(PATH).status_code == 503
    assert client.post(PATH, json={}).status_code == 503
    assert _count(x) == 0 and x.requests == [] and x.client.key_authority.ensure_calls == before


def test_browser_entry_requires_fixed_origin_and_closed_bounded_body(http):
    _, client = http
    assert client.get('/text-analysis-reissue/entry?code=private').status_code == 400
    assert client.get('/text-analysis-reissue/entry', headers={'Host': 'foreign.invalid'}).status_code == 421
    for raw, headers, expected in (
        (b'{"code":"' + b'A'*43 + b'"}', {'Origin': 'https://foreign.invalid'}, 403),
        (b'{"code":"A","code":"B"}', {'Origin': ORIGIN}, 422),
        (b' '*257, {'Origin': ORIGIN}, 413),
    ):
        response = client.post(PATH + '/browser-entry/consume', content=raw, headers={'Content-Type': 'application/json', **headers})
        assert response.status_code == expected


def test_entry_expiry_replacement_and_capacity_preserve_prepared_operations():
    now = [0.0]
    entries = ReissueNavigationEntries(clock=lambda: now[0])
    url = 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43
    first = entries.issue(('owner', 'one'), url)
    replacement = entries.issue(('owner', 'one'), url)
    with pytest.raises(ValueError):
        entries.consume(first)
    now[0] = 30
    with pytest.raises(ValueError):
        entries.consume(replacement)
    codes = [entries.issue(('owner', str(index)), url) for index in range(64)]
    with pytest.raises(ValueError):
        entries.issue(('owner', 'overflow'), url)
    assert entries.consume(codes[0]) == url
    entries.issue(('owner', 'replacement'), url)
    for invalid in ('http://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43,
                    url + '?scope=foreign', url + '#private', 'https://user@example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43):
        with pytest.raises(ValueError):
            entries.issue(('owner', 'bad'), invalid)
