"""Keep bounded question verification available after a long publication."""

from unittest.mock import Mock

import pytest

from app.analytics.errors import ProjectionUnavailable
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup


def test_completed_publication_refreshes_an_expired_identity(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    now = [0.0]
    f.source._identity_cache._clock = lambda: now[0]
    publish = f.stores.projections.publish_generation
    def slow_publication(*args, **kwargs):
        result = publish(*args, **kwargs)
        now[0] += 61
        return result
    monkeypatch.setattr(f.stores.projections, 'publish_generation', slow_publication)
    try:
        result = f.pipeline.project_account(ACCOUNT)
        with f.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=2))) as scope:
            assert scope.identity.content_digest == result.artifact.projection.canonical_content_digest
        assert now[0] == 61
    finally:
        cleanup(f)


def test_failed_optional_refresh_does_not_undo_publication(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    refresh = Mock(side_effect=RuntimeError('synthetic refresh failure'))
    monkeypatch.setattr(f.source, 'refresh_identity_cache', refresh)
    try:
        result = f.pipeline.project_account(ACCOUNT)
        refresh.assert_called_once_with(ACCOUNT)
        assert result.changed
        assert f.stores.projections.get(ACCOUNT) == result.artifact.projection
    finally:
        cleanup(f)


def test_post_publication_edit_cannot_keep_the_old_identity(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    refresh = f.source.refresh_identity_cache
    def mutate_then_refresh(account):
        with f.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='Changed after publication'")
        refresh(account)
    monkeypatch.setattr(f.source, 'refresh_identity_cache', mutate_then_refresh)
    try:
        result = f.pipeline.project_account(ACCOUNT)
        assert f.source.read_identity(ACCOUNT).content_digest != result.artifact.projection.canonical_content_digest
        assert f.stores.projections.get(ACCOUNT) is None
    finally:
        cleanup(f)
