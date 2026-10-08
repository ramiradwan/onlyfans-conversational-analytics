"""An exact stored prefix keeps its complete proof; only new rows need parsing."""
from unittest.mock import Mock
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture
from tests.test_append_enrichment_copying import active_unit
from app.analytics import conversation_enrichment_unit_sql as storage

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]


def test_verified_append_does_not_recreate_every_old_message_model(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = storage._validate_unit
        full = Mock(wraps=original)
        monkeypatch.setattr(storage, '_validate_unit', full)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert not any(call.args[0].header.message_count == 1025
                       and call.kwargs.get('materialize') is not True
                       for call in full.call_args_list)
        # The independent complete validator must agree with the prefix check.
        assert original(active_unit(f), check=lambda: None, materialize=False) == []
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


from dataclasses import replace


@pytest.fixture(scope='module')
def prefix_units(tmp_path_factory):
    f = dominant_fixture(tmp_path_factory.mktemp('prefix-unit'))
    try:
        f.pipeline.project_account(ACCOUNT)
        previous = active_unit(f)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        yield previous, active_unit(f)
    finally:
        cleanup(f)


def check_prefix(monkeypatch, old, current, *, check=lambda: None):
    monkeypatch.setattr(storage, 'load_unit', lambda *args: old)
    return storage._validate_appended_unit(None, 'old-generation', old.header, current, check=check)


def test_exact_prefix_agrees_with_full_content_validation(prefix_units, monkeypatch):
    old, current = prefix_units
    assert check_prefix(monkeypatch, old, current)
    assert storage._validate_unit(current, materialize=False) == []


@pytest.mark.parametrize('field', ['account_ref', 'conversation_ref', 'config_digest',
                                  'expires_at', 'first_source_at', 'sentiment', 'unit_id'])
def test_changed_header_cannot_use_a_prefix_proof(prefix_units, monkeypatch, field):
    from datetime import timedelta
    from app.analytics.conversation_enrichment_units import ConfidenceTotal
    old, current = prefix_units
    value = getattr(current.header, field)
    changed = (value + timedelta(seconds=1) if field.endswith('_at') else
               ConfidenceTotal(0, '0', '1') if field == 'sentiment' else 'changed')
    current = replace(current, header=replace(current.header, **{field: changed}))
    assert not check_prefix(monkeypatch, old, current)


@pytest.mark.parametrize('fault', ['changed_prefix', 'duplicate_tail', 'changed_analyzer', 'corrupt_bytes'])
def test_changed_actual_bytes_cannot_use_a_prefix_proof(prefix_units, monkeypatch, fault):
    import hashlib, json, zlib
    from app.analytics.conversation_enrichment_units import message_records, analyzer_records
    old, current = prefix_units
    if fault == 'corrupt_bytes':
        current = replace(current, messages=b'not compressed')
        with pytest.raises(ValueError):
            check_prefix(monkeypatch, old, current)
        return
    rows = list(message_records(current))
    analyzers = list(analyzer_records(current))
    if fault == 'changed_prefix':
        item = json.loads(rows[0]); item['direction'] = 'outbound'
        rows[0] = json.dumps(item, sort_keys=True, separators=(',', ':')).encode()
    elif fault == 'duplicate_tail':
        item = json.loads(rows[-1]); item['message_ref'] = json.loads(rows[0])['message_ref']
        rows[-1] = json.dumps(item, sort_keys=True, separators=(',', ':')).encode()
    else:
        analyzers[0] += b' '
    message_raw, analyzer_raw = b'\n'.join(rows), b'\n'.join(analyzers)
    current = replace(current, messages=zlib.compress(message_raw), analyzers=zlib.compress(analyzer_raw),
        header=replace(current.header, canonical_digest=hashlib.sha256(message_raw).hexdigest(),
                       analyzer_digest=hashlib.sha256(analyzer_raw).hexdigest()))
    assert not check_prefix(monkeypatch, old, current)


def test_cancelled_prefix_validation_cannot_succeed(prefix_units, monkeypatch):
    old, current = prefix_units
    def cancelled():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        check_prefix(monkeypatch, old, current, check=cancelled)


def test_missing_actual_predecessor_requires_full_validation(prefix_units, monkeypatch):
    old, current = prefix_units
    monkeypatch.setattr(storage, 'load_unit', lambda *args: None)
    assert not storage._validate_appended_unit(None, 'missing', old.header, current, check=lambda: None)


def test_small_conversation_append_uses_checked_prefix_bytes(tmp_path, monkeypatch):
    from tests.continuous_analytics_fixture import make_fixture
    f = make_fixture(tmp_path, conversations=3, messages=128)
    try:
        f.pipeline.project_account(ACCOUNT)
        checked = Mock(wraps=storage._validate_appended_frames)
        full = Mock(wraps=storage._validate_unit)
        monkeypatch.setattr(storage, '_validate_appended_frames', checked)
        monkeypatch.setattr(storage, '_validate_unit', full)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'small-new-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert checked.called
        assert not any(call.args[0].header.message_count == 129
                       and call.kwargs.get('materialize') is not True for call in full.call_args_list)
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


@pytest.mark.parametrize("escaped", [False, True])
def test_noncanonical_predecessor_cannot_hide_duplicate_tail(prefix_units, monkeypatch, escaped):
    import hashlib, json, zlib
    from app.analytics.conversation_enrichment_units import message_records, _canonical, _unit_id
    old, current = prefix_units
    old_rows = [row.replace(b'"message_ref":', b'"message_ref": ') for row in message_records(old)]
    if escaped:
        old_rows = [row.replace(b"m1:", br"\u006d1:") for row in old_rows]
    tail = json.loads(message_records(current)[-1])
    tail['message_ref'] = json.loads(old_rows[0])['message_ref']
    def reencode(unit, rows, analyzers):
        raw = b'\n'.join(rows)
        digest = hashlib.sha256(raw).hexdigest()
        analyzer_digest = hashlib.sha256(zlib.decompress(analyzers)).hexdigest()
        metrics = hashlib.sha256(_canonical(unit.header.metrics.model_dump(mode='json'))).hexdigest()
        header = replace(unit.header, canonical_digest=digest, analyzer_digest=analyzer_digest,
            unit_id=_unit_id(digest, analyzer_digest, metrics, len(rows)))
        return replace(unit, header=header, messages=zlib.compress(raw), analyzers=analyzers)
    previous = reencode(old, old_rows, old.analyzers)
    duplicate = reencode(current, [*old_rows, _canonical(tail)], old.analyzers)
    assert storage._validate_unit(previous, materialize=False) == []
    with pytest.raises(ValueError, match='source_invalid'):
        storage._validate_unit(duplicate, materialize=False)
    assert not check_prefix(monkeypatch, previous, duplicate)


def test_unique_canonical_prefix_does_not_decode_old_rows(prefix_units, monkeypatch):
    import json
    old, current = prefix_units
    decode = Mock(wraps=json.loads)
    monkeypatch.setattr(json, 'loads', decode)
    assert check_prefix(monkeypatch, old, current)
    decode.assert_not_called()
