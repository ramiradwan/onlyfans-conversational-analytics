"""Compare streaming graph bytes with an independent canonical JSON encoding."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.analytics.graph_privacy import graph_content_digest
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


def test_record_stream_matches_whole_document_encoding(tmp_path):
    f = make_fixture(tmp_path)
    try:
        value = f.pipeline._build(ACCOUNT, f.source.account_read_model(ACCOUNT), projection_generation=1)
        nodes, edges = value.nodes, value.edges
        expected = {'nodes': [n.model_dump(mode='json') for n in sorted(nodes, key=lambda n:n.node_id)],
                    'edges': [e.model_dump(mode='json') for e in sorted(edges, key=lambda e:e.edge_id)]}
        digest = 'sha256:' + hashlib.sha256(json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert graph_content_digest(list(reversed(nodes)), list(reversed(edges))) == digest
        invalid = nodes[0].model_copy(update={'properties': {'raw_message': 'not permitted'}})
        with pytest.raises(ValidationError):
            graph_content_digest([invalid], [])
    finally:
        cleanup(f)


def test_empty_graph_bytes_are_stable():
    assert graph_content_digest([],[]) == 'sha256:' + hashlib.sha256(b'{"edges":[],"nodes":[]}').hexdigest()
