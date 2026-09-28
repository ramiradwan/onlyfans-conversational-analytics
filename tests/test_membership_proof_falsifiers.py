"""An incomplete predecessor proof cannot authorize whole-unit reuse."""
from dataclasses import replace
from unittest.mock import Mock
import pytest
from app.analytics import conversation_integrity_store as integrity
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture


@pytest.mark.parametrize('damage', ['empty', 'omitted', 'duplicate'])
def test_partial_segment_proof_keeps_complete_membership_validation(tmp_path, monkeypatch, damage):
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = integrity.verify_generation_integrity
        def verify(*args, **kwargs):
            graph = kwargs.get('graph_validation')
            if graph is not None and graph.proof is not None:
                segments = graph.proof.segments
                changed = () if damage == 'empty' else segments[1:] if damage == 'omitted' else segments + (segments[0],)
                kwargs['graph_validation'] = replace(graph, proof=replace(graph.proof, segments=changed))
            return original(*args, **kwargs)
        monkeypatch.setattr(integrity, 'verify_generation_integrity', verify)
        restored = Mock(wraps=integrity.groups_for_members)
        monkeypatch.setattr(integrity, 'groups_for_members', restored)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'proof-falsifier', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert len({call.args[0].header.conversation_ref for call in restored.call_args_list}) == 3
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)
