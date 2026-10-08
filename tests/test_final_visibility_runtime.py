"""Regression controls for the final visibility candidate's dominant append path."""
import pytest
from tests.continuous_analytics_fixture import ACCOUNT, NOW, insert_message, advance, cleanup, cold_equal
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier("integration")]


def test_required_dominant_inbound_append_never_runs_full_metric_scan(tmp_path, monkeypatch):
    from app.analytics import conversation_append as append
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        def forbidden(*args, **kwargs):
            raise AssertionError('verified dominant inbound append used complete historical scan')
        monkeypatch.setattr(append, 'message_records', forbidden)
        monkeypatch.setattr(append, 'build_conversation_metrics', forbidden)
        monkeypatch.setattr(append, 'sentiment_score_sum', forbidden)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','required-dominant-inbound',NOW,2)
            advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        cold_equal(f,result.artifact)
    finally: cleanup(f)


def test_new_response_sample_keeps_complete_metric_fallback(tmp_path, monkeypatch):
    from app.analytics import conversation_append as append
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        # First add an inbound tail so the next outbound tail creates a response-time sample.
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','inbound-boundary',NOW,2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        calls=[]
        original=append.build_conversation_metrics
        def observed(*args,**kwargs):
            calls.append(True)
            return original(*args,**kwargs)
        monkeypatch.setattr(append,'build_conversation_metrics',observed)
        def forbidden_score_scan(*args, **kwargs):
            raise AssertionError('response fallback redundantly scanned historical sentiment first')
        monkeypatch.setattr(append,'sentiment_score_sum',forbidden_score_scan)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','outbound-response',NOW.replace(microsecond=1),3)
            advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        assert calls, 'median response-time changes require the complete historical calculation'
        cold_equal(f,result.artifact)
    finally: cleanup(f)


def test_verified_dominant_append_hashes_only_predecessor_input(tmp_path, monkeypatch):
    from app.analytics import conversation_append as append
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original=append.conversation_digest
        calls=[]
        def observed(value):
            calls.append(len(value['messages']))
            return original(value)
        monkeypatch.setattr(append,'conversation_digest',observed)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','digest-reuse-tail',NOW,2)
            advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        assert calls == [1024], calls
        cold_equal(f,result.artifact)
    finally: cleanup(f)


def test_verified_append_does_not_revalidate_predecessor_id_frame(tmp_path, monkeypatch):
    from app.analytics import conversation_integrity
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        def forbidden(*args,**kwargs):
            raise AssertionError('construction repeated complete predecessor membership verification')
        monkeypatch.setattr(conversation_integrity,'groups_for_members',forbidden)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','trusted-membership-tail',NOW,2)
            advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        cold_equal(f,result.artifact)
    finally: cleanup(f)


def test_streamed_enrichment_frame_matches_existing_encoding():
    import hashlib
    import zlib
    from app.analytics.conversation_enrichment_units import _compress_digest_parts
    parts=(b'{"a":1}',b'\n',b'{"b":2}')
    raw=b''.join(parts)
    compressed,digest=_compress_digest_parts(parts)
    assert zlib.decompress(compressed)==raw
    assert digest==hashlib.sha256(raw).hexdigest()
    assert compressed==zlib.compress(raw,1)


def test_sentiment_score_frame_parser_preserves_ordered_float_sum():
    from app.analytics.conversation_enrichment_units import sentiment_score_sum
    frame = b'{"sentiment":{"score":1.0}}\n{"sentiment":{"score":-0.5}}\n{"sentiment":{"score":0.333333}}'
    assert sentiment_score_sum(frame, 3) == 1.0 + -0.5 + 0.333333
    with pytest.raises(ValueError, match='sentiment_frame_invalid'):
        sentiment_score_sum(frame, 2)
    with pytest.raises(ValueError, match='sentiment_frame_invalid'):
        sentiment_score_sum(b'{"sentiment":{"score":oops}}', 1)


def test_retained_rollback_cleanup_skips_write_transaction(tmp_path, monkeypatch):
    from app.analytics.opaque_refs import account_ref
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','cleanup-preflight-tail',NOW,2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        store=getattr(f.stores.projections,'_store',None) or f.stores.projections
        with store.database.read() as db:
            assert db.execute("SELECT COUNT(*) FROM projection_generations WHERE status='retired'").fetchone()[0]==1
        def forbidden(*args,**kwargs):
            raise AssertionError('zero-work cleanup opened a write transaction')
        monkeypatch.setattr(store.database,'transaction',forbidden)
        assert store.collect_garbage(account_ref(ACCOUNT))==0
    finally: cleanup(f)


def test_required_dominant_inbound_append_skips_exact_sentiment_rescan(tmp_path, monkeypatch):
    from app.analytics import conversation_append as append
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        def forbidden(*args, **kwargs):
            raise AssertionError('safe dominant append rescanned the complete sentiment frame')
        monkeypatch.setattr(append,'sentiment_score_sum',forbidden)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','sentiment-fast-tail',NOW,2)
            advance(db)
        result=f.pipeline.project_account(ACCOUNT)
        cold_equal(f,result.artifact)
    finally: cleanup(f)


def test_noop_generation_gc_does_not_open_write_transaction(tmp_path, monkeypatch):
    from app.analytics.opaque_refs import account_ref
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','gc-preflight-tail',NOW,2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        store=getattr(f.stores.projections,'_store',None) or f.stores.projections
        original=store.database.transaction
        def forbidden(*args,**kwargs):
            raise AssertionError('no-op garbage collection opened a write transaction')
        monkeypatch.setattr(store.database,'transaction',forbidden)
        assert store.collect_garbage(account_ref(ACCOUNT))==0
        monkeypatch.setattr(store.database,'transaction',original)
    finally: cleanup(f)
