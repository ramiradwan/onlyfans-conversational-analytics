"""Keep source reads behind explicit session CSRF checks on every question route."""

import pytest

from tests.test_analytics_query_endpoints import ready, stored


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/v1/insights/questions'),
    ('POST', '/api/v1/insights/questions/evidence'),
    ('DELETE', '/api/v1/insights/questions/evidence'),
])
@pytest.mark.parametrize('token', [None, 'invalid'])
def test_question_route_rejects_bad_csrf_before_parameters_or_storage(ready, monkeypatch, method, path, token):
    def forbidden(*args, **kwargs):
        raise AssertionError('source access before CSRF verification')
    for operation in ('execute', 'resolve', 'discard'):
        monkeypatch.setattr(ready.resources, operation, forbidden)
    ready.client.headers.pop('x-csrf-token', None)
    if token is not None:
        ready.client.headers['x-csrf-token'] = token
    response = ready.client.request(method, path+'?creator_account_id=synthetic-other',
                                    json={'unexpected': 'synthetic'})
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'
    ready.scheduler.request_recovery.assert_not_called()
