"""Membership reuse has bounded framing and exact persisted-selection checks."""
from hashlib import sha256
from types import SimpleNamespace
import json
import sqlite3
import zlib
import pytest
from app.analytics import conversation_membership_validation as validation

pytestmark = [pytest.mark.ci_tier("integration")]


def identities(kind='node', count=300):
    prefix = 'g1:' if kind == 'node' else 'e1:'
    return [prefix + f'{number:064x}' for number in range(count)]


def packed(values):
    return zlib.compress(json.dumps(values, separators=(',', ':')).encode(), 1)


@pytest.mark.parametrize('kind', ['node', 'edge'])
@pytest.mark.parametrize('chunk_size', [1, 69, 70, 71, 517])
def test_frame_membership_across_compressed_and_record_boundaries(monkeypatch, kind, chunk_size):
    monkeypatch.setattr(validation, 'PAGE_BYTES', chunk_size)
    values = identities(kind, 128)
    data = packed(values)
    assert validation.contains_changed_member(data, len(values), kind, {values[0]}, lambda: None) is True
    assert validation.contains_changed_member(data, len(values), kind, {values[-1]}, lambda: None) is True
    assert validation.contains_changed_member(data, len(values), kind, {('g1:' if kind == 'node' else 'e1:') + 'f' * 64}, lambda: None) is False


@pytest.mark.parametrize('fault', ['truncated', 'trailing', 'concatenated', 'invalid', 'wrong_count', 'wrong_kind', 'whitespace', 'escaped', 'empty_mismatch', 'bomb'])
def test_unsupported_or_damaged_encoding_requires_full_verification(fault):
    values = identities(count=2)
    data = packed(values)
    count, kind = len(values), 'node'
    if fault == 'truncated': data = data[:-1]
    elif fault == 'trailing': data += b'after'
    elif fault == 'concatenated': data += packed(values)
    elif fault == 'invalid': data = b'not zlib'
    elif fault == 'wrong_count': count += 1
    elif fault == 'wrong_kind': kind = 'edge'
    elif fault == 'whitespace': data = zlib.compress(json.dumps(values).encode())
    elif fault == 'escaped': data = zlib.compress(json.dumps(values).replace('g1:', 'g\\u0031:').encode())
    elif fault == 'empty_mismatch': data = packed([])
    else: data = zlib.compress(b' ' * 100000)
    absent = {'g1:' + 'f' * 64}
    assert validation.contains_changed_member(data, count, kind, absent, lambda: None) is None


def test_empty_membership_and_cancellation():
    assert validation.contains_changed_member(packed([]), 0, 'edge', {'e1:' + 'f' * 64}, lambda: None) is False
    def cancelled(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        validation.contains_changed_member(packed(identities()), 300, 'node', {'g1:' + 'f' * 64}, cancelled)


def segment(kind, records, name):
    digest = sha256(('graph-segment.v1:' + kind + ':00').encode())
    for key, value in sorted(records.items()):
        digest.update(key.encode() + b':' + value.encode() + b'\n')
    return SimpleNamespace(kind=kind, bucket='00', count=len(records), digest=digest.hexdigest(), segment_id=name)


def selection(old, new, kind='node'):
    db = sqlite3.connect(':memory:')
    db.execute(f'CREATE TABLE graph_segment_{kind}s(creator_account_id,segment_id,{kind}_id,content_id)')
    db.executemany(f'INSERT INTO graph_segment_{kind}s VALUES (?,?,?,?)', [('account', 'old', k, v) for k, v in old.items()])
    previous = segment(kind, old, 'old')
    current = segment(kind, new, 'new')
    prepared = {(kind, 'new'): [{kind + '_id': k, 'content_id': v} for k, v in sorted(new.items())]}
    return db, {(kind, '00'): previous}, {(kind, '00'): current}, prepared


@pytest.mark.parametrize('change', ['none', 'add', 'replace', 'remove', 'whole_segment'])
def test_exact_selected_versions_determine_only_invalidated_old_members(change):
    keys = identities(count=4)
    old = {keys[0]: 'a' * 64, keys[1]: 'b' * 64}
    new = dict(old)
    if change == 'add': new[keys[2]] = 'c' * 64
    elif change == 'replace': new[keys[0]] = 'd' * 64
    elif change == 'remove': del new[keys[0]]
    elif change == 'whole_segment': new = {}
    db, before, current, prepared = selection(old, new)
    try:
        if change == 'whole_segment': current = {}
        changes = validation.changed_predecessor_members(db, 'account', before, current, prepared, lambda: None)
        expected = set(old) if change == 'whole_segment' else {keys[0]} if change in ('replace', 'remove') else set()
        assert changes == {'node': expected, 'edge': set()}
    finally: db.close()


@pytest.mark.parametrize('fault', ['candidate_content', 'old_content', 'account', 'candidate_count'])
def test_unverified_or_wrong_selected_metadata_cannot_supply_a_preservation_result(fault):
    keys = identities(count=3)
    db, before, current, prepared = selection({keys[0]: 'a' * 64}, {keys[0]: 'a' * 64, keys[1]: 'b' * 64})
    try:
        account = 'account'
        if fault == 'candidate_content': prepared[('node', 'new')][0]['content_id'] = 'c' * 64
        elif fault == 'old_content': db.execute('UPDATE graph_segment_nodes SET content_id=?', ('c' * 64,))
        elif fault == 'account': account = 'wrong'
        else: current[('node', '00')].count += 1
        with pytest.raises(ValueError):
            validation.changed_predecessor_members(db, account, before, current, prepared, lambda: None)
    finally: db.close()


@pytest.mark.parametrize('limit', ['records', 'bytes', 'changes', 'no_prepared', 'no_transaction'])
def test_capacity_and_ownership_refusal_select_complete_fallback(monkeypatch, limit):
    keys = identities(count=4)
    db, before, current, prepared = selection({keys[0]: 'a' * 64, keys[1]: 'b' * 64}, {keys[2]: 'c' * 64, keys[3]: 'd' * 64})
    try:
        if limit == 'records': monkeypatch.setattr(validation, 'MAX_CHANGED_VERIFICATION_ROWS', 1)
        elif limit == 'bytes': monkeypatch.setattr(validation, 'MAX_CHANGED_VERIFICATION_BYTES', 1)
        elif limit == 'changes': monkeypatch.setattr(validation, 'PAGE_RECORDS', 1)
        elif limit == 'no_prepared': prepared = None
        else: db.commit()
        assert validation.changed_predecessor_members(db, 'account', before, current, prepared, lambda: None) is None
    finally: db.close()



def full_selection_case(spec, conversation='cold-selection'):
    from app.analytics.conversation_integrity import summarize_group
    from app.analytics.opaque_refs import account_ref, conversation_ref

    account = account_ref('synthetic-full-selection')
    chat = conversation_ref('synthetic-full-selection', conversation)
    summaries, members, current, records = {}, {}, {}, {}
    for kind, bucket, count in sorted(spec):
        prefix = 'g1:' if kind == 'node' else 'e1:'
        selected = tuple(prefix + bucket + f'{number:062x}' for number in range(count))
        versions = {identity: sha256(identity.encode()).hexdigest() for identity in selected}
        key = kind, bucket
        summaries[key] = summarize_group(account, chat, *key, versions)
        members[key] = selected
        current[key] = 'verified-segment-' + kind + '-' + bucket
        records.update(versions)
    return account, chat, summaries, members, current, records


@pytest.mark.parametrize('count, expected', [(256, [256]), (257, [256, 1])])
def test_full_group_lookup_batches_at_identity_bound(monkeypatch, count, expected):
    from app.analytics import conversation_integrity_store as store, shared_graph

    spec = [('node', f'{bucket:02x}', count - 255 if bucket == 0 else 1)
            for bucket in range(256)]
    account, chat, summaries, members, current, records = full_selection_case(spec)
    calls = []
    def lookup(db, generation, actual_account, kind, keys, check, *, page_layout):
        assert generation == 'witnessed-generation' and actual_account == account
        assert kind == 'node' and page_layout is True
        calls.append(tuple(keys))
        return {key: records[key] for key in keys}
    def fallback(*args):
        raise AssertionError('small bounded group used the large-group fallback')
    monkeypatch.setattr(shared_graph, 'selected_content_ids', lookup)
    store._verify_cold_groups(None, 'witnessed-generation', account, chat,
        summaries, members, current, fallback, lambda: None)
    assert [len(keys) for keys in calls] == expected
    assert [key for keys in calls for key in keys] == [
        key for selected in members.values() for key in selected]


@pytest.mark.parametrize('limit', ['large_group', 'bytes'])
def test_full_selection_capacity_retains_per_group_validation(monkeypatch, limit):
    from app.analytics import conversation_integrity_store as store, shared_graph

    spec = [('node', '00', 257)] if limit == 'large_group' else [
        ('node', '00', 2), ('node', '01', 2)]
    account, chat, summaries, members, current, records = full_selection_case(spec)
    if limit == 'bytes':
        monkeypatch.setattr(store, 'MAX_FULL_SELECTION_BYTES', 1)
    monkeypatch.setattr(shared_graph, 'selected_content_ids',
        lambda *args, **kwargs: pytest.fail('capacity refusal must use original selection'))
    calls = []
    def fallback(kind, bucket, selected):
        calls.append((kind, bucket, tuple(selected)))
        return {identity: records[identity] for identity in selected}
    store._verify_cold_groups(None, 'generation', account, chat,
        summaries, members, current, fallback, lambda: None)
    assert [(kind, bucket) for kind, bucket, _ in calls] == list(summaries)
    def missing(*args):
        return {}
    with pytest.raises(ValueError, match='conversation_integrity_selected_content_changed'):
        store._verify_cold_groups(None, 'generation', account, chat,
            summaries, members, current, missing, lambda: None)


def test_full_selections_do_not_cross_kind_or_conversation(monkeypatch):
    from app.analytics import conversation_integrity_store as store, shared_graph

    calls = []
    for conversation in ('first-conversation', 'second-conversation'):
        account, chat, summaries, members, current, records = full_selection_case(
            [('edge', '00', 2), ('node', '00', 2)], conversation)
        def lookup(db, generation, actual_account, kind, keys, check, *, page_layout):
            assert actual_account == account and generation == 'generation'
            assert all(identity.startswith('e1:' if kind == 'edge' else 'g1:') for identity in keys)
            calls.append((chat, kind))
            return {identity: records[identity] for identity in keys}
        monkeypatch.setattr(shared_graph, 'selected_content_ids', lookup)
        store._verify_cold_groups(None, 'generation', account, chat,
            summaries, members, current, lambda *args: pytest.fail('unexpected fallback'),
            lambda: None)
    assert [kind for _, kind in calls] == ['edge', 'node', 'edge', 'node']
    assert calls[0][0] == calls[1][0] and calls[2][0] == calls[3][0]
    assert calls[0][0] != calls[2][0]


def test_missing_segment_flushes_prior_groups_before_original_refusal(monkeypatch):
    from app.analytics import conversation_integrity_store as store, shared_graph

    account, chat, summaries, members, current, records = full_selection_case(
        [('node', '00', 1), ('node', '01', 1), ('node', '02', 1)])
    del current[('node', '01')]
    calls = []
    def lookup(db, generation, actual_account, kind, keys, check, *, page_layout):
        calls.append(tuple(keys))
        return {identity: records[identity] for identity in keys}
    monkeypatch.setattr(shared_graph, 'selected_content_ids', lookup)
    def fallback(kind, bucket, selected):
        assert (kind, bucket) == ('node', '01')
        raise ValueError('conversation_integrity_segment_missing')
    with pytest.raises(ValueError, match='conversation_integrity_segment_missing'):
        store._verify_cold_groups(None, 'generation', account, chat,
            summaries, members, current, fallback, lambda: None)
    assert calls == [members[('node', '00')]]


@pytest.mark.parametrize('phase', ['before_lookup', 'after_lookup'])
def test_full_selection_cancellation_preserves_original_base_exception(monkeypatch, phase):
    from app.analytics import conversation_integrity_store as store, shared_graph

    class Cancelled(BaseException):
        pass
    cancelled = Cancelled()
    account, chat, summaries, members, current, records = full_selection_case(
        [('node', '00', 1), ('node', '01', 1)])
    fetched = False
    calls = []
    def check():
        if phase == 'before_lookup' or fetched:
            raise cancelled
    def lookup(db, generation, actual_account, kind, keys, check, *, page_layout):
        nonlocal fetched
        calls.append(tuple(keys))
        fetched = True
        return {identity: records[identity] for identity in keys}
    monkeypatch.setattr(shared_graph, 'selected_content_ids', lookup)
    with pytest.raises(Cancelled) as raised:
        store._verify_cold_groups(None, 'generation', account, chat,
            summaries, members, current, lambda *args: pytest.fail('unexpected fallback'), check)
    assert raised.value is cancelled
    assert len(calls) == (0 if phase == 'before_lookup' else 1)



def test_full_selection_flushes_at_cumulative_byte_bound(monkeypatch):
    from app.analytics import conversation_integrity_store as store, shared_graph

    monkeypatch.setattr(store, 'MAX_FULL_SELECTION_BYTES', 8 * 1024)
    account, chat, summaries, members, current, records = full_selection_case(
        [('node', '00', 2), ('node', '01', 2), ('node', '02', 2)])
    calls = []
    def lookup(db, generation, actual_account, kind, keys, check, *, page_layout):
        calls.append(tuple(keys))
        return {identity: records[identity] for identity in keys}
    monkeypatch.setattr(shared_graph, 'selected_content_ids', lookup)
    store._verify_cold_groups(None, 'generation', account, chat,
        summaries, members, current, lambda *args: pytest.fail('each group fits the byte cap'),
        lambda: None)
    assert [len(keys) for keys in calls] == [4, 2]
    assert [identity for keys in calls for identity in keys] == [
        identity for selected in members.values() for identity in selected]


def test_cold_batch_retains_every_original_group_summary_in_order(monkeypatch):
    from app.analytics import conversation_integrity_store as store, shared_graph

    account, chat, summaries, members, current, records = full_selection_case(
        [('node', '00', 1), ('node', '01', 1)])
    records[members[('node', '01')][0]] = '0' * 64
    checked = []
    summarize = store.summarize_group
    def observed(actual_account, conversation, kind, bucket, versions, check):
        checked.append((actual_account, conversation, kind, bucket))
        return summarize(actual_account, conversation, kind, bucket, versions, check)
    monkeypatch.setattr(store, 'summarize_group', observed)
    monkeypatch.setattr(shared_graph, 'selected_content_ids',
        lambda db, generation, actual_account, kind, keys, check, **kwargs:
            {identity: records[identity] for identity in keys})
    with pytest.raises(ValueError, match='conversation_integrity_selected_content_changed'):
        store._verify_cold_groups(None, 'generation', account, chat,
            summaries, members, current, lambda *args: pytest.fail('unexpected fallback'),
            lambda: None)
    assert checked == [(account, chat, 'node', '00'), (account, chat, 'node', '01')]
