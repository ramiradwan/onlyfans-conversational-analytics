"""An independently matched append does not enumerate nonexistent removals."""
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta

from app.analytics.opaque_refs import conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]


def test_exact_append_loads_each_predecessor_unit_once(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = f.stores.projections.open_conversation_fragments
        reads = Counter()
        @contextmanager
        def traced(account):
            with original(account) as load:
                previous = load.previous_graph_unit
                def read(reference):
                    reads[reference] += 1
                    return previous(reference)
                load.previous_graph_unit = read
                yield load
        monkeypatch.setattr(f.stores.projections, 'open_conversation_fragments', traced)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'single-new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert reads == Counter({conversation_ref(ACCOUNT, 'chat-0'): 1}), reads
        # Other conversations remain covered by the account catalog and persisted proofs;
        # the deletion test below proves they cannot disappear behind this fast path.
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_append_does_not_hide_other_conversation_removals(tmp_path):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            db.execute("DELETE FROM account_messages WHERE chat_id='chat-1'")
            insert_message(db, 'chat-0', 'single-new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert conversation_ref(ACCOUNT, 'chat-1') not in {
            metric.conversation_ref for metric in result.artifact.projection.conversation_metrics}
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)
