"""Count actual graph payload bytes through construction and publication."""
from datetime import timedelta
import pytest
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, insert_message, cleanup, cold_equal
from tests.test_dominant_append_reuse import dominant_fixture

@pytest.mark.parametrize('prefix_size', [1024, 4096])
def test_verified_append_payload_work_stays_in_changed_buckets(tmp_path, monkeypatch, record_property, prefix_size):
    from app.analytics import shared_graph
    from app.analytics.opaque_refs import account_ref
    f = dominant_fixture(tmp_path)
    try:
        if prefix_size > 1024:
            with f.repositories.database.transaction() as db:
                for i in range(1024, prefix_size):
                    insert_message(db, 'chat-0', f'dominant-{i}', NOW-timedelta(hours=2)+timedelta(seconds=i), i)
        f.pipeline.project_account(ACCOUNT)
        with f.stores.database.read() as db:
            total = db.execute("SELECT SUM(length(c.canonical_bytes)) FROM graph_segment_chunks c JOIN generation_graph_segments m USING(creator_account_id,segment_id,kind) JOIN projection_generations g USING(creator_account_id,generation_id) WHERE g.status='active' AND g.creator_account_id=?", (account_ref(ACCOUNT),)).fetchone()[0]
        original, opened = shared_graph.verified_segment_chunk, []
        def observe(*args, **kwargs):
            value = original(*args, **kwargs)
            if value is not None:
                opened.append((args[-2], args[-1], len(value[1])))
            return value
        monkeypatch.setattr(shared_graph, 'verified_segment_chunk', observe)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'bounded-integrity-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        calls, used = len(opened), sum(item[2] for item in opened)
        assert 0 < calls <= 32, calls
        assert 0 < used < total / 4, (used, total)
        record_property('prefix_messages', prefix_size)
        record_property('predecessor_payload_calls', calls)
        record_property('predecessor_payload_bytes', used)
        record_property('whole_predecessor_payload_bytes', total)
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)
