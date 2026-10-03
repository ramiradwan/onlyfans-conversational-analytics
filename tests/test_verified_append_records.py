"""Copies from checked canonical chunks keep exact bytes and bounded metadata."""
from unittest.mock import Mock
import pytest

from app.analytics import conversation_append
from app.analytics.opaque_refs import conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, cleanup
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier('integration')]


def test_append_copies_verified_records_without_decoding_property_objects(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as loader:
            unit = loader.previous_graph_unit(conversation_ref(ACCOUNT, 'chat-0'))
            expected = conversation_append._previous_graph(loader, unit, lambda: None)
            decoder = Mock(side_effect=AssertionError('do not decode already checked property objects'))
            monkeypatch.setattr(conversation_append.json, 'JSONDecoder', decoder)
            actual = conversation_append._previous_graph(loader, unit, lambda: None)
        assert actual.nodes == expected.nodes and actual.edges == expected.edges
        assert actual.node_counts == expected.node_counts
        assert actual.edge_counts == expected.edge_counts
        if unit.header.checksum_version == 2:
            from app.analytics.conversation_integrity import from_graph
            assert from_graph(actual.account_ref, unit.header.conversation_ref, actual)[0] == unit.header.graph_digest
        else:
            assert actual.digest(check=lambda: None) == unit.header.graph_digest
        decoder.assert_not_called()
    finally:
        cleanup(f)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_record_framing_preserves_escaped_properties_and_exact_bytes(kind):
    from collections import Counter
    from types import SimpleNamespace
    import hashlib, json
    account = 'a1:' + 'a' * 64
    rows = []
    for index in range(3):
        row = {'account_ref': account, kind + '_id': f'id:{index}',
               'occurred_at': None, 'properties': {
                   'text': 'quoted },{"account_ref":"not a record"} and €',
                   'note': 'a fake ,"relation":"fake","sequence":0'}}
        row.update({'kind': 'message'} if kind == 'node' else
                   {'relation': 'contains', 'sequence': index, 'source_id': 'a', 'target_id': 'b'})
        rows.append(row)
    texts = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for row in rows]
    encoded = ','.join(texts).encode()
    categories = (('message' if kind == 'node' else 'contains', len(rows)),)
    segment = SimpleNamespace(kind=kind, chunk_digest=hashlib.sha256(encoded).hexdigest(),
                              count=len(rows), categories=categories)
    actual = list(conversation_append._checked_record_spans(segment, encoded, account, lambda: None))
    assert [row[2] for row in actual] == texts
    assert tuple(sorted(Counter(row[1] for row in actual).items())) == categories


@pytest.mark.parametrize('fault', ['digest', 'count', 'category', 'account', 'trailing', 'cancelled'])
def test_record_framing_rejects_changed_bytes_metadata_and_cancellation(fault):
    from types import SimpleNamespace
    import hashlib
    data = b'{"account_ref":"a1:account","kind":"message","node_id":"id:1","occurred_at":null,"properties":{}}'
    segment = SimpleNamespace(kind='node', chunk_digest=hashlib.sha256(data).hexdigest(),
                              count=1, categories=(('message', 1),))
    account = 'a1:account'
    if fault == 'digest':
        segment.chunk_digest = '0' * 64
    elif fault == 'count':
        segment.count = 2
    elif fault == 'category':
        segment.categories = (('conversation', 1),)
    elif fault == 'account':
        account = 'different'
    elif fault == 'trailing':
        data += b',not-a-record'
        segment.chunk_digest = hashlib.sha256(data).hexdigest()
    def check():
        if fault == 'cancelled':
            raise ValueError('cancelled')
    with pytest.raises(ValueError):
        list(conversation_append._checked_record_spans(segment, data, account, check))


def test_chunk_framing_matches_only_record_headers(monkeypatch):
    """Do not run a backtracking header pattern across every payload byte."""
    from types import SimpleNamespace
    import hashlib
    original = conversation_append._RECORD_START
    calls = []
    def match(text, offset=0):
        calls.append(offset)
        return original.match(text, offset)
    def forbidden(*args):
        raise AssertionError('whole-chunk regular-expression scan')
    monkeypatch.setattr(conversation_append, '_RECORD_START',
        SimpleNamespace(match=match, finditer=forbidden))
    records = [('{"account_ref":"a1:test","kind":"message","node_id":"id:%d",'
                '"occurred_at":null,"properties":{"note":"%s"}}' % (i, 'plain ' * 1000))
               for i in range(3)]
    data = ','.join(records).encode()
    segment = SimpleNamespace(kind='node', chunk_digest=hashlib.sha256(data).hexdigest(),
                              count=3, categories=(('message', 3),))
    actual = list(conversation_append._checked_record_spans(segment, data, 'a1:test', lambda: None))
    assert [row[2] for row in actual] == records
    assert len(calls) == 3
