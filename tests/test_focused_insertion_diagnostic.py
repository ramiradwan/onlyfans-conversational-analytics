"""The focused diagnostic preserves real content checks and cannot qualify A07."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import analytics_insertion_diagnostic as focused
from tools import light_first_update_benchmark as runner

CLOCK = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
ACCOUNT = 'synthetic-continuous-owner'


@pytest.fixture
def fixture():
    raw = focused.raw_fixture(1000, CLOCK)
    previous = focused.full_unit(raw, ACCOUNT, CLOCK-timedelta(days=90))
    db = focused.component_database(previous)
    try:
        yield raw, previous, db
    finally:
        db.close()


def test_fixture_separates_append_and_actual_tied_insertion():
    raw = focused.raw_fixture(100000, CLOCK)
    assert len(raw['messages']) == 50001
    inserted = focused.mutate_raw(raw, 'insert')
    appended = focused.mutate_raw(raw, 'append')
    assert [r['message_id'] for r in inserted['messages'][-2:]] == [
        'visibility-idle-dominant', 'visibility-ordinary-dominant']
    assert [r['message_id'] for r in appended['messages'][-2:]] == [
        'visibility-ordinary-dominant', 'visibility-z-append-dominant']
    assert inserted['messages'][-1]['sent_at'] == inserted['messages'][-2]['sent_at']
    assert [r['source_ordinal'] for r in inserted['messages']] == list(range(50002))
    assert len(raw['messages']) == 50001  # No accumulated mutation across samples.


@pytest.mark.parametrize('operation', ['append', 'insert'])
def test_real_stored_validators_and_fresh_oracle_agree(fixture, operation):
    raw, previous, db = fixture
    trace = focused.Attribution()
    trace.install()
    try:
        result = focused.component_sample(db, previous, focused.mutate_raw(raw, operation),
                                          operation, ACCOUNT, trace, index=0)
    finally:
        trace.restore()
    assert result['accepted_path'] == operation
    assert result['independent_rebuild_equal'] and result['persisted_content_revalidated']
    assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM conversation_enrichment_refs').fetchone()[0] == 1
    assert result['construct_seconds'] > 0 and result['validation_seconds'] > 0
    if operation == 'insert':
        match = next(e for e in trace.events if e['name'].endswith('.match_inserted_source'))
        assert match['counts']['previous_records'] == 501
        assert match['counts']['reconstructed_metric_inputs'] == 501
        assert match['counts']['shifted_records'] == 1


def test_oracle_runs_again_for_each_sample(fixture):
    raw, previous, db = fixture
    original = focused.full_unit
    with patch.object(focused, 'full_unit', wraps=original) as oracle:
        for index in range(2):
            focused.component_sample(db, previous, focused.mutate_raw(raw, 'insert'),
                                     'insert', ACCOUNT, focused.Attribution(enabled=False), index=index)
    assert oracle.call_count == 2


def test_no_trace_installs_no_global_profiler_and_retains_outer_timing(fixture):
    import sys
    raw, previous, db = fixture
    trace = focused.Attribution(enabled=False)
    before = sys.getprofile()
    result = focused.component_sample(db, previous, focused.mutate_raw(raw, 'append'),
                                      'append', ACCOUNT, trace, index=0)
    assert sys.getprofile() is before
    assert trace.events == []
    assert result['construct_seconds'] > 0


def test_changed_oracle_cannot_produce_success(fixture):
    raw, previous, db = fixture
    expected = focused.full_unit(focused.mutate_raw(raw, 'insert'), ACCOUNT, previous.header.retention_cutoff)
    with patch.object(focused, 'full_unit', return_value=replace(expected,
            header=replace(expected.header, canonical_digest='0'*64))):
        with pytest.raises(ValueError, match='focused_independent_rebuild_mismatch'):
            focused.component_sample(db, previous, focused.mutate_raw(raw, 'insert'),
                                     'insert', ACCOUNT, focused.Attribution(), index=0)
    assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 1


def test_source_edit_fails_exact_insertion_binding(fixture):
    raw, previous, _ = fixture
    changed = focused.mutate_raw(raw, 'insert')
    changed['messages'][0]['text'] = 'Different source'
    with pytest.raises(ValueError, match='focused_insertion_match_rejected'):
        focused.construct(previous, changed, 'insert', ACCOUNT, lambda: None)


@pytest.mark.parametrize('mutation', ['metrics', 'account', 'ordinal', 'payload'])
def test_persisted_candidate_mutations_are_not_accepted(fixture, mutation):
    from app.analytics.conversation_enrichment_insertion import validate_inserted_unit
    from app.analytics import conversation_enrichment_units as units
    import hashlib, json
    raw, previous, db = fixture
    candidate = focused.construct(previous, focused.mutate_raw(raw, 'insert'), 'insert', ACCOUNT, lambda: None)
    if mutation == 'metrics':
        candidate = replace(candidate, header=replace(candidate.header,
            metrics=candidate.header.metrics.model_copy(update={'turn_count': 999})))
    elif mutation == 'account':
        candidate = replace(candidate, header=replace(candidate.header, account_ref='a1:'+'0'*64))
    else:
        rows = list(units.message_records(candidate))
        value = json.loads(rows[-2])
        if mutation == 'ordinal':
            value['source_ordinal'] = 0
        else:
            value['direction'] = 'outbound'
        rows[-2] = units._canonical(value)
        frame = b'\n'.join(rows)
        candidate = replace(candidate, messages=units._compress(frame),
                            header=replace(candidate.header, canonical_digest=hashlib.sha256(frame).hexdigest()))
    try:
        valid = validate_inserted_unit(db, 'previous', previous.header, candidate, check=lambda: None)
    except (ValueError, TypeError):
        valid = False
    assert valid is False


def test_cancellation_and_patch_restoration(fixture):
    from app.analytics import conversation_insertion as insertion
    raw, previous, _ = fixture
    original = insertion.match_inserted_source
    trace = focused.Attribution()
    def cancelled():
        raise RuntimeError('cancelled')
    try:
        trace.install()
        with pytest.raises(RuntimeError, match='cancelled'):
            focused.construct(previous, focused.mutate_raw(raw, 'insert'), 'insert', ACCOUNT, cancelled)
    finally:
        trace.restore()
    assert insertion.match_inserted_source is original
    assert not trace.patches
    assert any(e.get('error') == 'RuntimeError' for e in trace.events)


def test_nested_spans_report_self_time_and_cap():
    trace = focused.Attribution()
    with trace.span('outer'):
        with trace.span('inner'):
            pass
    parent = next(e for e in trace.events if e['name'] == 'outer')
    child = next(e for e in trace.events if e['name'] == 'inner')
    assert child['parent'] == parent['id']
    assert abs(parent['seconds'] - parent['self_seconds'] - child['seconds']) < 1e-9
    trace.sequence = focused.MAX_EVENTS
    with pytest.raises(RuntimeError, match='capacity'):
        with trace.span('overflow'):
            pass


def test_focused_cli_does_not_relax_existing_recipe():
    base = ['runner', '--source-root', 'source', '--expected-sha', 'sha', '--output', 'out', '--owner-lock', 'lock']
    with patch('sys.argv', base + ['--preparation', 'cold', '--messages', '10000']):
        with pytest.raises(SystemExit): runner.options()
    with patch('sys.argv', base + ['--preparation', 'focused-update', '--messages', '10000']):
        assert runner.options().messages == 10000
    for invalid in (['--seed', 'seed'], ['--idle-seconds', '61'], ['--baseline-operation', 'probe']):
        with patch('sys.argv', base + ['--preparation', 'focused-component', *invalid]):
            with pytest.raises(SystemExit): runner.options()


def test_generator_and_coroutine_construction_are_not_work_timings():
    def stream():
        yield 1
    async def task():
        return 1
    owner = SimpleNamespace(__name__='fixture', stream=stream, task=task)
    trace = focused.Attribution()
    for name in ('stream', 'task'):
        with pytest.raises(ValueError, match='synchronous_bulk_call'):
            trace.patch(owner, name)
    assert not trace.patches
    assert not trace.events


def test_component_none_does_not_install_attribution(tmp_path, monkeypatch):
    from pathlib import Path
    from tools import analytics_qualification as q
    manifest = q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    def forbidden(*args, **kwargs):
        raise AssertionError('unprofiled run installed a patch')
    monkeypatch.setattr(focused.Attribution, 'install', forbidden)
    args = SimpleNamespace(messages=1000, trace_mode='none', focused_repeats=1, output=tmp_path)
    result = {}
    light = SimpleNamespace(atomic_status=lambda *a, **k: None)
    asyncio.run(focused.run_component(args, q, light, None, tmp_path/'status.json', manifest, result, tmp_path/'work'))
    assert result['complete'] is True
    assert result['attribution'] == []
    assert len(result['samples']) == 2
    assert result['component_database_closed'] is True
