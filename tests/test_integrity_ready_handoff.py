"""Verified readiness metadata does not reopen immutable unit payload rows."""
from tests.continuous_analytics_fixture import ACCOUNT, cleanup


def test_ready_generation_upgrade_check_uses_verified_headers(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tests.continuous_analytics_fixture import make_fixture
    f = make_fixture(tmp_path, conversations=2, messages=4)
    try:
        f.pipeline.project_account(ACCOUNT)
        statements = []
        original = f.stores.database.read
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db
        monkeypatch.setattr(f.stores.database, 'read', traced)
        assert not f.stores.projections.integrity_upgrade_required(ACCOUNT)
        assert not any('conversation_graph_units' in sql for sql in statements), statements
    finally:
        cleanup(f)
