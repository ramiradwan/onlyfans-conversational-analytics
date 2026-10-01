"""An append retains complete checked units without a second copy of their pages."""
from datetime import timedelta

import pytest

from app.analytics.opaque_refs import account_ref, conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier('integration')]


def page_count(f):
    with f.stores.database.read() as db:
        row = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()
        parameters = (row[0], account_ref(ACCOUNT), conversation_ref(ACCOUNT, 'chat-0'))
        return db.execute("SELECT COUNT(*) FROM conversation_page_sets WHERE generation_id=? "
            "AND creator_account_id=? AND conversation_ref=?", parameters).fetchone()[0]


def append(f, sequence):
    with f.repositories.database.transaction() as db:
        insert_message(db, 'chat-0', f'append-{sequence}', NOW - timedelta(minutes=1) + timedelta(seconds=sequence), sequence)
        advance(db)
    return f.pipeline.project_account(ACCOUNT)


def test_dominant_append_does_not_write_duplicate_conversation_pages(tmp_path):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = [analyzer.calls for analyzer in f.analyzers]
        result = append(f, 0)
        assert page_count(f) == 0
        with f.stores.database.read() as db:
            generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
            for table in ('conversation_graph_refs', 'conversation_enrichment_refs'):
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE generation_id=? AND "
                    "creator_account_id=? AND conversation_ref=?", (generation, account_ref(ACCOUNT),
                    conversation_ref(ACCOUNT, 'chat-0'))).fetchone()[0] == 1
        cold_equal(f, f.stores.projections.get_artifact(ACCOUNT))
        result = append(f, 1)
        assert page_count(f) == 0
        assert [a.calls - n for a, n in zip(f.analyzers, before)] == [2, 2, 2]
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


@pytest.mark.parametrize('limit', ['MAX_GRAPH_UNITS', 'MAX_ENRICHMENT_UNITS'])
def test_missing_complete_units_keep_the_page_fallback(tmp_path, monkeypatch, limit):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        monkeypatch.setattr('app.analytics.conversation_reuse.' + limit, 0)
        result = append(f, 0)
        assert page_count(f) == 1
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)
