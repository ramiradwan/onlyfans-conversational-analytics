"""Visibility measurements start the same recovery/maintenance lifecycle as runtime."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tools import analytics_qualification_worker as worker
from tools.analytics_qualification_fixture import Journal

pytestmark = [pytest.mark.ci_tier('fast')]


@pytest.mark.asyncio
async def test_initial_visibility_process_keeps_periodic_maintenance_enabled(tmp_path, monkeypatch):
    starts = []
    class Scheduler:
        detached_worker_count = 0
        retained_account_count = 0
        def __init__(self, *args, **kwargs):
            assert kwargs['reconciliation_interval'] == 30
        async def start(self, *, recover):
            starts.append(recover)
        async def close(self, **kwargs):
            return True
    async def prepared(*args):
        return None
    monkeypatch.setattr('app.analytics.scheduling.InProcessProjectionScheduler', Scheduler)
    monkeypatch.setattr(worker, 'direct', prepared)
    work = SimpleNamespace(size=0, clock=datetime.now(timezone.utc), close=lambda: None,
        f=SimpleNamespace(source=object(), pipeline=object()),
        manifest={'visibility': {'process_cases': [[]]}})
    result = await worker.visibility(work, Journal(tmp_path/'events', 'test'), {'repeat': 0}, 'test')
    assert result['complete']
    assert starts == [True]
