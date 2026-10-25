"""Frozen execution refuses test settings while retained reads remain usable."""
from __future__ import annotations

import sys

import pytest

from app.analytics.nlp_contribution_replay import replay_contribution
from app.analytics.nlp_findings_store import FindingsStore
from app.core.config import settings
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_knowledge_contributions import create, data, seal  # noqa: F401
from tests.test_nlp_processing_publication import runner

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.mark.parametrize("entry", ["process_one", "_process_one"])
def test_frozen_worker_refuses_mutable_test_environment(tmp_path, monkeypatch, entry):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        worker, profile, adapter = runner(fixture)
        assert worker.execution_ready(profile)
        with monkeypatch.context() as patch:
            patch.setattr(settings, "environment", "test")
            patch.setattr(sys, "frozen", True, raising=False)
            with pytest.raises(ValueError, match="^findings_execution_unavailable$"):
                getattr(worker, entry)(ACCOUNT)
            assert adapter.requests == []
            assert worker.findings.state(ACCOUNT)["completion_revision"] == 0
    finally:
        cleanup(fixture)


def test_frozen_canonical_commit_refuses_valid_staged_output(data, monkeypatch):
    _, repository, spec, _, _, frames, policy = data
    create(data)
    token = repository.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    for ordinal, frame in enumerate(frames):
        repository.stage_member(token, ordinal, frame, policy=policy)

    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(ValueError, match="^findings_execution_unavailable$"):
        repository.commit(token, policy=policy)
    with pytest.raises(ValueError, match="contribution_unavailable"):
        repository.get_committed(spec.contribution_id, policy=policy)


@pytest.mark.parametrize("environment", ["test", "production"])
def test_frozen_saved_read_and_replay_need_no_execution(data, monkeypatch, environment):
    fixture, repository, spec, _, profile, _, policy = data
    _, manifest = seal(data)
    findings = FindingsStore(fixture.stores.database)
    findings.select(ACCOUNT, profile, mode="paused")

    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert repository.get_committed(spec.contribution_id, policy=policy) == manifest
    page = replay_contribution(repository, findings, spec.contribution_id, policy=policy, limit=64)
    assert page["complete"] and page["imported"] == 2
    assert repository.export_document(spec.contribution_id, policy=policy)["manifest"] == manifest.model_dump(mode="json")
