"""Bulk visibility observation must not inherit a short interactive poll cutoff."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from tools import analytics_qualification_workloads as workloads

pytestmark = [pytest.mark.ci_tier('fast')]


@pytest.mark.asyncio
async def test_bulk_observer_survives_the_old_sample_cutoff(monkeypatch):
    from app.analytics.errors import CanonicalRevisionChanged
    from app.models.analytics import AvailabilityStatus
    state = {"revision": 1, "ready": False}
    def reject(*args):
        raise CanonicalRevisionChanged()
    work = SimpleNamespace(account="synthetic", last=SimpleNamespace(generation_id="generation"),
        events=[], cleaned={}, observe_activation=lambda: None,
        counts=lambda: {"messages": 1000, "revision": state["revision"]}, verify=lambda: {},
        f=SimpleNamespace(stores=SimpleNamespace(projections=SimpleNamespace(read_generation_artifact=reject))),
        manifest={"limits": {"whole_worker_seconds": 5},
                  "measurement": {"visibility_poll_seconds": 0.001, "maximum_visibility_observations": 2}})
    def mutate():
        state["revision"] = 2
        return time.monotonic()
    def observe(*args):
        return {"current": state["ready"], "at": time.monotonic(), "error": None if state["ready"] else "analytics_question_limit_exceeded"}
    class Scheduler:
        retained_account_count = 0
        async def schedule(self, *args):
            pass
        async def wait(self, *args):
            await asyncio.sleep(0.05)
            work.events.append({"stage": "activated", "generation": "generation", "at": time.monotonic()})
            work.cleaned["generation"] = time.monotonic()
            state["ready"] = True
            return SimpleNamespace(availability=AvailabilityStatus.AVAILABLE, attempted_revision=2)
    events = []
    journal = SimpleNamespace(save=lambda label, value: events.append((label, value)))
    monkeypatch.setattr(workloads, "observed_question", observe)
    result = await workloads.scheduled(work, journal, None, Scheduler(), "100_edits", mutate)
    assert result["valid_current_result"]
    assert result["clocks"]["first_valid_visible_result"] >= result["clocks"]["activation"]
    assert any(label == "backlog-drained" for label, _ in events)

    observations = [value for label, value in events if label == "visibility-observation"]
    assert observations[0]["error"] == "analytics_question_limit_exceeded"
    assert observations[-1]["current"] is True
    assert len(observations) == 2
