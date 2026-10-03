"""Terminal-tie metric reuse preserves full ordering and persisted verification."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random

import pytest

from tools import analytics_insertion_diagnostic as diagnostic
from app.analytics import conversation_insertion as insertion
from app.analytics import conversation_enrichment_insertion as validation
from app.analytics import conversation_enrichment_units as units
from app.analytics import tied_insertion_metrics
from app.analytics.metrics import build_conversation_metrics_from_bound_values
from app.models.analytics import MessageEnrichment, MessageDirection

pytestmark = [pytest.mark.ci_tier("integration")]

ACCOUNT = 'synthetic-continuous-owner'
CLOCK = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def pair():
    raw = diagnostic.raw_fixture(1000, CLOCK)
    previous = diagnostic.full_unit(raw, ACCOUNT, CLOCK-timedelta(days=90))
    new = diagnostic.mutate_raw(raw, 'insert')
    candidate = diagnostic.construct(previous, new, 'insert', ACCOUNT, lambda: None)
    db = diagnostic.component_database(previous)
    try:
        yield raw, previous, new, candidate, db
    finally:
        db.close()


def test_real_terminal_tie_reuses_metrics_without_full_reconstruction(pair, monkeypatch):
    raw, previous, new, candidate, db = pair
    def forbidden(*args, **kwargs):
        raise AssertionError('terminal tie reconstructed whole metric history')
    monkeypatch.setattr(insertion, 'metric_input', forbidden)
    actual = diagnostic.construct(previous, new, 'insert', ACCOUNT, lambda: None)
    assert actual == candidate
    assert validation.validate_inserted_unit(db, 'previous', previous.header, actual, check=lambda: None)
    expected = diagnostic.full_unit(new, ACCOUNT, previous.header.retention_cutoff)
    assert actual.header == expected.header
    assert units.message_frame(actual) == units.message_frame(expected)


def test_full_source_checks_remain_when_metric_objects_are_deferred(pair):
    raw, previous, new, _, _ = pair
    for index in (0, 250, 501):
        changed = dict(new, messages=[dict(row) for row in new['messages']])
        changed['messages'][index]['text'] = 'Edited source'
        assert insertion.match_inserted_source(ACCOUNT, changed, previous.header,
                                               units.message_records(previous), lambda: None) is None


def test_general_tie_keeps_full_metric_fallback(pair, monkeypatch):
    _, previous, new, _, db = pair
    new['messages'][-2]['direction'] = 'outbound'
    calls = []
    original = insertion.metric_input
    def counted(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(insertion, 'metric_input', counted)
    actual = diagnostic.construct(previous, new, 'insert', ACCOUNT, lambda: None)
    assert len(calls) == previous.header.message_count+1
    calls.clear()
    assert validation.validate_inserted_unit(db, 'previous', previous.header, actual, check=lambda: None)
    assert len(calls) == previous.header.message_count+1
    expected = diagnostic.full_unit(new, ACCOUNT, previous.header.retention_cutoff)
    assert actual.header == expected.header


def rehashed(unit, rows, **changes):
    frame = b'\n'.join(rows)
    h = replace(unit.header, **changes, canonical_digest=hashlib.sha256(frame).hexdigest())
    h = replace(h, unit_id=units._unit_id(h.canonical_digest, h.analyzer_digest,
        hashlib.sha256(units._canonical(h.metrics.model_dump(mode='json'))).hexdigest(), h.message_count))
    return replace(unit, header=h, messages=units._compress(frame))


@pytest.mark.parametrize('fault', ['prefix', 'suffix_direction', 'suffix_classification', 'suffix_account',
    'suffix_timestamp', 'suffix_ordinal', 'inserted_duplicate', 'metric', 'confidence', 'config', 'retention'])
def test_rehashed_terminal_tie_tampering_is_rejected(pair, fault):
    _, previous, _, candidate, db = pair
    rows = list(units.message_records(candidate))
    changes = {}
    position = -1
    if fault == 'prefix':
        position = 0
    if fault == 'inserted_duplicate':
        position = -2
    value = json.loads(rows[position])
    if fault == 'prefix': value['direction'] = 'outbound'
    elif fault == 'suffix_direction': value['direction'] = 'outbound'
    elif fault == 'suffix_classification': value['sentiment']['score'] = .321
    elif fault == 'suffix_account': value['account_ref'] = 'a1:'+'0'*64
    elif fault == 'suffix_timestamp': value['sent_at'] = (CLOCK-timedelta(days=1)).isoformat()
    elif fault == 'suffix_ordinal': value['source_ordinal'] -= 1
    elif fault == 'inserted_duplicate': value['message_ref'] = json.loads(rows[0])['message_ref']
    elif fault == 'metric': changes['metrics'] = candidate.header.metrics.model_copy(update={'turn_count': 999})
    elif fault == 'confidence': changes['sentiment'] = replace(candidate.header.sentiment, numerator='0')
    elif fault == 'config': changes['config_digest'] = 'sha256:'+'0'*64
    else: changes['retention_cutoff'] = candidate.header.first_source_at
    rows[position] = units._canonical(value)
    bad = rehashed(candidate, rows, **changes)
    assert not validation.validate_inserted_unit(db, 'previous', previous.header, bad, check=lambda: None)


def test_escaped_duplicate_in_proved_prefix_is_detected(pair):
    _, previous, _, candidate, db = pair
    from app.analytics import conversation_enrichment_unit_sql as storage
    # A legal escaped frame is still digest checked. The added identity must
    # not evade the prefix duplicate check through a different JSON encoding.
    old_rows = list(units.message_records(previous))
    encoded = old_rows[0].replace(b'm1:', b'\\u006d1:')
    assert encoded != old_rows[0]
    old_rows[0] = encoded
    old = rehashed(previous, old_rows)
    # Inject a separately digest-bound legal predecessor through the reader;
    # immutable production rows are never updated or bypassed by this test.
    from unittest.mock import patch
    rows = list(units.message_records(candidate));rows[0] = encoded
    v = json.loads(rows[-2]);v['message_ref'] = json.loads(old_rows[0])['message_ref']
    rows[-2] = units._canonical(v)
    bad = rehashed(candidate, rows)
    with patch.object(storage, 'load_unit', return_value=old):
        assert not validation.validate_inserted_unit(db, 'previous', old.header, bad, check=lambda: None)


def test_cancellation_is_observed_in_bounded_suffix(pair):
    _, previous, _, candidate, _ = pair
    rows = list(units.message_records(candidate))
    added = MessageEnrichment.model_validate_json(rows[-2])
    suffix = [MessageEnrichment.model_validate_json(rows[-1])]
    def check(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        tied_insertion_metrics.tied_suffix_metrics(previous.header.metrics, suffix, added, check)


def test_terminal_tie_equivalence_over_directions_positions_scores_and_history(pair):
    _, previous, _, candidate, _ = pair
    template = MessageEnrichment.model_validate_json(units.message_records(candidate)[-1])
    rng = random.Random(70421)
    accepted = 0
    for trial in range(160):
        count, tail = rng.randint(2, 28), rng.randint(1, 5)
        tail = min(tail, count)
        direction = rng.choice([MessageDirection.INBOUND, MessageDirection.OUTBOUND])
        score = rng.choice([0.0, 0.1, -0.2, .999999, -.000001, .3333333333333])
        values = []
        for i in range(count):
            tied = i >= count-tail
            item = template.model_copy(update={
                'sent_at': CLOCK if tied else CLOCK-timedelta(seconds=count-i),
                'source_ordinal': i,
                'direction': direction if tied else rng.choice([MessageDirection.INBOUND, MessageDirection.OUTBOUND]),
                'sentiment': template.sentiment.model_copy(update={'score': score if tied else rng.uniform(-1,1)})})
            values.append(item)
        old_metrics = build_conversation_metrics_from_bound_values(template.account_ref, template.conversation_ref,
            template.participant_ref, 0, [insertion.metric_input(v.model_dump(mode='json')) for v in values])
        index = count-tail+rng.randrange(tail)
        added = template.model_copy(update={'sent_at':CLOCK,'source_ordinal':index,'direction':direction,
                                            'sentiment':template.sentiment.model_copy(update={'score':score})})
        suffix = [v.model_copy(update={'source_ordinal':v.source_ordinal+1}) for v in values[index:]]
        result = tied_insertion_metrics.tied_suffix_metrics(old_metrics, suffix, added, lambda: None)
        expected = build_conversation_metrics_from_bound_values(template.account_ref, template.conversation_ref,
            template.participant_ref, 0, [insertion.metric_input(v.model_dump(mode='json'))
                                         for v in values[:index]+[added]+suffix])
        if result is not None:
            accepted += 1
            assert result == expected
    assert accepted > 30


@pytest.mark.parametrize('change', ['direction', 'score', 'timestamp', 'account', 'ordinal', 'size'])
def test_ambiguous_suffix_uses_full_fallback(pair, change):
    _, previous, _, candidate, _ = pair
    rows = list(units.message_records(candidate))
    added = MessageEnrichment.model_validate_json(rows[-2])
    last = MessageEnrichment.model_validate_json(rows[-1])
    suffix = [last]
    if change == 'direction': last = last.model_copy(update={'direction':'outbound'})
    elif change == 'score': last = last.model_copy(update={'sentiment':last.sentiment.model_copy(update={'score':.8})})
    elif change == 'timestamp': last = last.model_copy(update={'sent_at':CLOCK-timedelta(seconds=1)})
    elif change == 'account': last = last.model_copy(update={'account_ref':'wrong'})
    elif change == 'ordinal': last = last.model_copy(update={'source_ordinal':0})
    else: suffix = [last] * (tied_insertion_metrics.PAGE_RECORDS+1)
    if change != 'size': suffix = [last]
    assert tied_insertion_metrics.tied_suffix_metrics(previous.header.metrics, suffix, added, lambda:None) is None


@pytest.mark.parametrize('value,allowed', [
    (b'{"sent_at":"date","source_ordinal":4}', True),
    (b'{"sent_at": "date", "source_ordinal": 4}', True),
    (b'{"sent_at":123,"source_ordinal":4}', False),
    (b'{"sent_at":"date","source_ordinal":40}', False),
    (b'{"sent_at":"date","source_ordinal":4.0}', False),
    (b'{"sent_at":"date","source_ordinal":"4"}', False),
    (b'{"sent_at":"date","source_ordinal":4,"source_ordinal" : 5}', False),
    (b'{"sent_at":"date","source_ordinal":4,"source_ordinal":5}', False),
    (b'{"sent_at":"date","source_ordinal":4,"source_ordin\\u0061l":5}', False),
    (b'{"sent_at":"date","source_ordinal":true}', False),
])
def test_legacy_field_admission_is_preserved(value, allowed):
    assert validation._ordinary_record_fields(value, 4) is allowed
