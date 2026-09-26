"""Measure synthetic local analytics work without claiming laptop qualification."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def peak_memory_bytes():
    if sys.platform != 'win32':
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    values = Counters()
    values.cb = ctypes.sizeof(values)
    kernel, psapi = ctypes.WinDLL('kernel32'), ctypes.WinDLL('psapi')
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(values), values.cb):
        raise OSError('process_memory_measurement_failed')
    return values.PeakWorkingSetSize


def process_io_counters():
    """Return native Windows process transfer counters, not database-only I/O."""

    if os.name != 'nt':
        return None
    import ctypes
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
            ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
             'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters)]
    kernel.GetProcessIoCounters.restype = ctypes.c_int
    value = Counters()
    if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(value)):
        return None
    return {name: int(getattr(value, name)) for name, _ in value._fields_}


def reference_artifact(fixture, generation: int, *, compact: bool):
    """Recompute from canonical sources without reading stored analytics outputs."""

    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.canonical_source import HistoryAnalyticsSource
    from tests.continuous_analytics_fixture import ACCOUNT

    cold = AnalyticsPipeline(fixture.source, clock=lambda: fixture.clock.now,
                             reuse_enrichment=False, reuse_conversations=False)
    if not compact:
        raw = HistoryAnalyticsSource(fixture.repositories.history).account_read_model(ACCOUNT)
        return cold._build(ACCOUNT, raw, projection_generation=generation)
    from app.analytics.conversation_reuse import ConversationBuild, ACTIVE_CONVERSATIONS

    state = ConversationBuild()
    state.compact = True
    token = ACTIVE_CONVERSATIONS.set(state)
    try:
        source = fixture.source.analytics_snapshot(ACCOUNT)
        return cold._build(ACCOUNT, source, projection_generation=generation)
    finally:
        ACTIVE_CONVERSATIONS.reset(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--messages', type=int, default=1000)
    parser.add_argument('--query-samples', type=int, default=100)
    parser.add_argument('--verification-mode', choices=('full', 'digests'), default='full')
    parser.add_argument('--skip-unchanged-rebuild', action='store_true',
                        help='Measure cold build and a real update without the forced unchanged diagnostic.')
    args = parser.parse_args()
    if not 100 <= args.messages <= 100_000 or not 1 <= args.query_samples <= 100:
        parser.error('Use 100 to 100000 messages and 1 to 100 query samples.')
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT))
    import tests.conftest  # Isolated test keys and paths; no customer stores are opened.
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, insert_message, advance
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.canonical_source import HistoryAnalyticsSource
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.errors import AnalyticsError
    from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
    import app.analytics.source_snapshot as snapshots
    now = datetime.now(timezone.utc) + timedelta(days=1)
    fixture = make_fixture(args.output/'data', conversations=101, messages=0)
    fixture.clock.now = now
    scan = snapshots.scan_identity
    scan_counts = Counter()
    def measured_scan(*values, **options):
        scan_counts['attempts'] += 1
        result = scan(*values, **options)
        scan_counts['completed_scans'] += 1
        scan_counts['messages'] += result[2]
        return result
    snapshots.scan_identity = measured_scan
    report = {'synthetic': True, 'complete': False, 'checkout_base_revision': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'working_tree': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True),
        'platform': platform.platform(), 'python': sys.version, 'processor': platform.processor(),
        'logical_cpus': os.cpu_count(), 'messages': args.messages, 'conversations': 101,
        'layout': 'half_in_one_conversation_remainder_across_100', 'evaluation_clock': now.isoformat(),
        'phases': [], 'laptop_qualified': False,
        'measurement': 'candidate_build_and_publication_without_explicit_artifact_read',
        'verification_mode': args.verification_mode,
        'reference_representation': 'compact_canonical_rebuild' if args.verification_mode == 'digests' else 'full_canonical_rebuild',
        'forced_unchanged_rebuild': not args.skip_unchanged_rebuild,
        'unmeasured': ['installer_size', 'disk_type', 'power_mode', 'constrained_laptop_profiles']}
    output = args.output/'report.json'
    def save():
        output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    save()
    try:
        with fixture.repositories.database.transaction() as db:
            for index in range(args.messages):
                chat = 0 if index < args.messages//2 else 1+(index-args.messages//2) % 100
                at = now-timedelta(hours=48)+timedelta(seconds=index*48*3600/args.messages)
                insert_message(db, f'chat-{chat}', f'input-{index}', at, index)
        def phase(name):
            fixture.source.loaded.clear()
            before, scans_before = sum(a.calls for a in fixture.analyzers), scan_counts.copy()
            start = time.perf_counter()
            before_io = process_io_counters()
            checkpoints = {}
            fixture.stores.projections.crash_hook = lambda stage, generation: checkpoints.setdefault(
                stage, time.perf_counter() - start)
            candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
            built = time.perf_counter()
            result = fixture.pipeline.publish_candidate(candidate)
            end = time.perf_counter()
            after_io = process_io_counters()
            item = {'phase': name, 'build_seconds': built-start, 'publication_seconds': end-built,
                'total_seconds': end-start, 'analyzer_calls': sum(a.calls for a in fixture.analyzers)-before,
                'conversation_body_reads': len(fixture.source.loaded),
                'checkpoints_seconds': checkpoints,
                'activation_seconds': checkpoints.get('activated'),
                'post_activation_seconds': ((end-start)-checkpoints['activated']
                    if 'activated' in checkpoints else None),
                'identity_work': dict(scan_counts-scans_before), 'process_peak_bytes': peak_memory_bytes()}
            item['process_io_delta'] = ({key: after_io[key] - before_io[key] for key in before_io}
                if before_io is not None and after_io is not None else None)
            report['phases'].append(item)
            save()
            print(json.dumps(item), flush=True)
            return result
        phase('cold')
        if not args.skip_unchanged_rebuild:
            phase('unchanged_rebuild')
        started = time.perf_counter()
        with fixture.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'added-message', now-timedelta(microseconds=1), 2)
            advance(db)
        report['ingest_seconds'] = time.perf_counter()-started
        updated_run = phase('one_new_message')
        report['publication_peak_bytes'] = peak_memory_bytes()
        policy = RuntimePolicy(AuthContext('synthetic-principal', ACCOUNT, 'creator'), AuthorizationEpoch(1))
        resources = QuestionResources(fixture.source, fixture.pipeline, clock=lambda: now)
        plan = {'question': 'no_later_creator_reply.v1', 'timezone': 'UTC',
            'start': (now-timedelta(hours=48)).isoformat(), 'end': now.isoformat(), 'cutoff': now.isoformat()}
        elapsed, failures, undetermined = [], Counter(), []
        try:
            for index in range(args.query_samples+5):
                started = time.perf_counter()
                error = None
                try:
                    result = resources.execute(policy, plan)
                except AnalyticsError as exception:
                    error = exception.code
                if index >= 5:
                    elapsed.append(time.perf_counter()-started)
                    if error is not None:
                        failures[error] += 1
                    else:
                        undetermined.append(result.page.undetermined_conversation_count)
        finally:
            resources.close()
        ordered = sorted(elapsed)
        report['query'] = {'samples': len(elapsed), 'failures': dict(failures),
            'p95_seconds_including_failures': ordered[math.ceil(0.95*len(ordered))-1],
            'max_seconds': max(elapsed), 'undetermined_counts': sorted(set(undetermined)),
            'production_message_types_qualified': False}
        save()
        read_started = time.perf_counter()
        updated = updated_run.artifact if args.verification_mode == 'full' else updated_run.reference
        if updated is None:
            raise AssertionError('stored_reference_required_for_digest_verification')
        report['artifact_materialization_seconds'] = (time.perf_counter() - read_started
            if args.verification_mode == 'full' else None)
        save()
        header = updated.projection if args.verification_mode == 'full' else updated
        expected = reference_artifact(fixture, header.projection_generation,
                                      compact=args.verification_mode == 'digests')
        matches = expected == updated if args.verification_mode == 'full' else (
            expected.projection.projection_digest == header.projection_digest
            and expected.projection.graph_digest == header.graph_digest
            and expected.projection.canonical_content_digest == header.canonical_content_digest)
        if not matches:
            raise AssertionError('incremental_result_differs_from_clean_rebuild')
        del expected, updated
        report['clean_rebuild_equal'] = True
        report['process_peak_bytes'] = peak_memory_bytes()
        report['complete'] = True
        save()
        print(json.dumps(report['query']), flush=True)
    except Exception as error:
        report['error_type'] = type(error).__name__
        save()
        raise
    finally:
        snapshots.scan_identity = scan
        cleanup(fixture)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
