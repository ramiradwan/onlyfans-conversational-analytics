"""Finite state budgets for one uninterrupted visibility qualification worker."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

SCHEMA = 'analytics-execution-state.v1'


def mark_state(config: dict, state: str, instance: str) -> None:
    policy = config.get('execution_schedule')
    if policy is None:
        return
    from tools import analytics_qualification as q
    index = policy['states'].index(state)
    target = Path(policy['directory']) / f'{index:02}-{state}.json'
    pending = target.with_suffix('.pending')
    q.write_once(pending, {
        'schema': SCHEMA, 'index': index, 'state': state,
        'at': time.monotonic(), 'process_instance': instance,
        'supervisor_instance': os.environ.get('OFCA_QUALIFICATION_PROCESS'),
    })
    os.link(pending, target)  # Publish complete bytes atomically without replacement.
    pending.unlink()


class StateBudget:
    def __init__(self, policy: dict, *, started: float, token: str):
        self.directory = Path(policy['directory'])
        self.states = tuple(policy['states'])
        self.seconds = policy['maximum_state_seconds']
        if (not self.states or len(set(self.states)) != len(self.states)
                or any(not isinstance(s, str) or not s.isidentifier() for s in self.states)
                or type(self.seconds) not in (int, float)
                or not math.isfinite(self.seconds) or self.seconds <= 0):
            raise ValueError('execution_schedule_invalid')
        self.started, self.token = started, token
        self.records, self.raw_records = [], []
        self.index, self.state_started = 0, started
        self.process_instance = None

    def poll(self, now: float, *, finished=False) -> None:
        names = [f'{i:02}-{state}.json' for i, state in enumerate(self.states)]
        files = sorted(self.directory.glob('*.json'))
        if [p.name for p in files] != names[:len(files)]:
            raise ValueError('execution_state_order_invalid')
        for i, path in enumerate(files):
            if path.stat().st_size > 4096:
                raise ValueError('execution_state_record_oversized')
            raw = path.read_bytes()
            if i < len(self.raw_records):
                if raw != self.raw_records[i]:
                    raise ValueError('execution_state_changed')
                continue
            from tools import analytics_qualification as q
            value = json.loads(raw)
            if q.encoded(value) != raw:
                raise ValueError("execution_state_encoding_invalid")
            at = value.get('at')
            instance = value.get('process_instance')
            if (value.get('schema') != SCHEMA or type(value.get('index')) is not int or value.get('index') != i
                    or value.get('state') != self.states[i]
                    or value.get('supervisor_instance') != self.token):
                raise ValueError('execution_state_binding_invalid')
            if (type(at) not in (int, float) or not math.isfinite(at)
                    or not self.state_started <= at <= now
                    or not isinstance(instance, str) or not instance
                    or self.process_instance not in (None, instance)):
                raise ValueError('execution_state_clock_or_process_invalid')
            if at - self.state_started > self.seconds:
                raise TimeoutError('execution_state_watchdog_expired')
            self.process_instance = instance
            self.records.append(value)
            self.raw_records.append(raw)
            if i:  # Fixture construction and interpreter launch are charged to cold.
                self.index, self.state_started = i, at
        if len(files) < len(self.records):
            raise ValueError('execution_state_removed')
        if now - self.state_started > self.seconds:
            raise TimeoutError('execution_state_watchdog_expired')
        if finished and len(self.records) != len(self.states):
            raise ValueError('execution_states_incomplete')

    def report(self, ended: float) -> dict:
        starts = [self.started] + [r['at'] for r in self.records[1:]]
        intervals = [{'state': self.states[i], 'started': start,
                      'ended': starts[i + 1] if i + 1 < len(starts) else ended}
                     for i, start in enumerate(starts)]
        return {'schema': SCHEMA, 'states': list(self.states),
                'maximum_state_seconds': self.seconds,
                'records': self.records, 'intervals': intervals,
                'supervisor_instance': self.token,
                'complete': len(self.records) == len(self.states)}


def check_evidence(manifest, config, result, names, read, events):
    """Recheck raw state boundaries and charge product/oracle clocks to them."""
    from tools import analytics_qualification as q
    expected = manifest.get('visibility_execution')
    if expected is None or 'probes' not in result.get('payload', {}):
        return []
    if config.get('mode') != 'visibility':
        return ['execution_mode_mismatch']
    schedule, report = config.get('execution_schedule', {}), result.get('execution') or {}
    if ({k: v for k, v in schedule.items() if k != 'directory'} != expected
            or report.get('schema') != SCHEMA or report.get('complete') is not True
            or report.get('states') != expected['states']
            or report.get('maximum_state_seconds') != expected['maximum_state_seconds']
            or report.get('supervisor_instance') != result.get('process_instance')):
        return ['execution_schedule_or_owner_mismatch']
    paths = [f'execution/{i:02}-{state}.json' for i, state in enumerate(expected['states'])]
    if sorted(name for name in names if name.startswith('execution/') and name.endswith('.json')) != paths:
        return ['execution_state_records_missing_or_extra']
    records = [read(path) for path in paths]
    intervals = report.get('intervals', [])
    if records != report.get('records') or len(intervals) != len(records):
        return ['execution_summary_differs_from_raw_records']
    original = read('process.json')['instance']
    ranges = {}
    for i, (record, interval, state) in enumerate(zip(records, intervals, expected['states'])):
        start, end, at = interval.get('started'), interval.get('ended'), record.get('at')
        if not all(q.finite(value) for value in (start, end, at)):
            return ['execution_state_clock_invalid']
        if (record.get('schema') != SCHEMA or type(record.get('index')) is not int or record.get('index') != i
                or record.get('state') != state or interval.get('state') != state
                or record.get('process_instance') != original
                or record.get('supervisor_instance') != result.get('process_instance')
                or not start <= at <= end or end - start > expected['maximum_state_seconds']
                or (i and (start != at or intervals[i - 1]['ended'] != start))):
            return ['execution_state_binding_or_deadline_invalid']
        ranges[state] = (start, end)
    elapsed = intervals[-1]['ended'] - intervals[0]['started']
    if (not q.finite(result.get('seconds')) or not elapsed <= result['seconds'] <= elapsed + 10
            or result.get('maximum_seconds') != expected['maximum_worker_seconds']):
        return ['execution_total_not_bound_to_worker']
    for event in events:
        value = event['value']
        phase = value.get('phase')
        state = ('cold' if phase == 'cold' else 'rebuilt' if phase == 'unchanged_rebuild'
                 else str(value.get('case', '')).split('/')[0])
        if state not in ranges:
            continue
        times = [value for value in value.get('clocks', {}).values() if value is not None]
        times.append(event['observed']['monotonic_ns'] / 1_000_000_000)
        if any(not q.finite(t) or not ranges[state][0] <= t <= ranges[state][1] for t in times):
            return ['operation_or_oracle_outside_execution_state']
    return []
