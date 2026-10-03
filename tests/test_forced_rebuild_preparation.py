"""Forced rebuild preparation does not retain duplicate complete projections."""
from unittest.mock import Mock

from tests.test_shared_graph import fixture
from tests.continuous_analytics_fixture import ACCOUNT, cold_equal

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]


def test_forced_rebuild_reads_one_current_projection(fixture, monkeypatch):
    first = fixture.pipeline.project_account(ACCOUNT).artifact
    read = Mock(wraps=fixture.stores.projections.get)
    monkeypatch.setattr(fixture.stores.projections, 'get', read)
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    assert read.call_count == 1
    assert result.artifact == first
    cold_equal(fixture, result.artifact)


def test_forced_rebuild_skips_only_unused_fast_path_graph_read(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    read = Mock(side_effect=AssertionError('unused no-op check'))
    monkeypatch.setattr(fixture.pipeline.graph, 'partition_revision', read)
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    read.assert_not_called()
    cold_equal(fixture, result.artifact)
