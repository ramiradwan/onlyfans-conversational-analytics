"""Membership reuse has bounded framing and exact persisted-selection checks."""
from hashlib import sha256
from types import SimpleNamespace
import json
import sqlite3
import zlib
import pytest
from app.analytics import conversation_membership_validation as validation


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
