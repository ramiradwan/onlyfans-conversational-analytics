"""Cold integrity validation must select unit members, not rescan account buckets."""
from unittest.mock import Mock

from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture

import pytest

pytestmark = [pytest.mark.ci_tier("integration")]


def test_cold_integrity_reads_only_requested_membership_metadata(tmp_path, monkeypatch):
    from app.analytics import shared_graph
    from app.analytics.conversation_integrity import MAX_GROUP_RECORDS
    select = Mock(wraps=shared_graph.selected_content_ids)
    monkeypatch.setattr(shared_graph, 'selected_content_ids', select)
    f = make_fixture(tmp_path, conversations=3, messages=4)
    try:
        f.pipeline.project_account(ACCOUNT)
        assert select.called, 'integrity validation scanned whole account buckets'
        assert all(0 < len(call.args[4]) <= MAX_GROUP_RECORDS for call in select.call_args_list)
    finally:
        cleanup(f)


def test_unchanged_assembly_reads_headers_not_membership_payloads(tmp_path, monkeypatch):
    import inspect
    from app.analytics import conversation_graph_unit_sql as units
    f = make_fixture(tmp_path, conversations=3, messages=8)
    try:
        f.pipeline.project_account(ACCOUNT)
        original, restored = units.load_unit, []
        def observe(*args, **kwargs):
            caller = inspect.currentframe().f_back
            if caller.f_code.co_name == 'previous_graph_unit':
                restored.append(args[3])
            return original(*args, **kwargs)
        monkeypatch.setattr(units, 'load_unit', observe)
        f.pipeline.publish_candidate(f.pipeline.build_candidate(ACCOUNT, force=True))
        assert not restored, {'unchanged_membership_payload_reads': len(restored)}
    finally:
        cleanup(f)
