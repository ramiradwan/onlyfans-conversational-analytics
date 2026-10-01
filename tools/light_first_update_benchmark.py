"""First-update diagnostic with ready-seed and qualification-prefix preparation.

Cold mode calls the unchanged qualification helpers in one process, then stops.
No full qualification, manual checkpoint, storage bypass, or production patch.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import contextmanager
from functools import partial, wraps
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import traceback


def options():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--expected-sha', required=True)
    p.add_argument('--seed', type=Path)
    p.add_argument('--preparation', choices=['ready', 'cold', 'repeat1-prefix', 'focused-component', 'focused-update'], default='ready')
    p.add_argument('--messages', type=int, choices=[1000, 10000, 100000], default=100000)
    p.add_argument('--baseline-operation', type=Path)
    p.add_argument('--timeout-seconds', type=float)
    p.add_argument('--trace-mode', choices=['none', 'coarse'], default='coarse')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--owner-lock', type=Path, required=True)
    p.add_argument('--case', choices=['small', 'dominant'], default='small')
    p.add_argument('--idle-seconds', type=float, default=0)
    p.add_argument('--verify-after', action='store_true')
    p.add_argument('--focused-repeats', type=int, choices=range(1, 6), default=3)
    p.add_argument('--focused-operation', choices=['append', 'insert'], default='insert')
    args = p.parse_args()
    if args.preparation == 'ready' and args.seed is None:
        p.error('--seed is required for ready preparation')
    if args.preparation != 'ready' and (args.seed is not None or args.idle_seconds):
        p.error('fresh preparation does not permit seed reuse or an inserted idle')
    if args.preparation == 'repeat1-prefix' and args.baseline_operation:
        p.error('repeat1-prefix cannot bind a v6 single-operation manifest as v7')
    if args.messages == 10000 and not args.preparation.startswith('focused-'):
        p.error('10000 messages is available only in focused diagnostics')
    if args.preparation.startswith('focused-') and args.baseline_operation:
        p.error('focused diagnostics cannot bind a qualification probe as their baseline')
    if args.timeout_seconds is None:
        args.timeout_seconds = 9000 if args.preparation == 'repeat1-prefix' else 1800
    if args.timeout_seconds <= 0:
        p.error('--timeout-seconds must be positive')
    return args


class Trace:
    def __init__(self):
        self.phase = 'setup'
        self.events = []
        self.patches = []

    def add(self, kind, started, **values):
        self.events.append(dict(kind=kind, phase=values.pop('phase', self.phase), start=started,
            end=time.monotonic(), thread=threading.get_ident(), **values))

    def patch(self, owner, name, replacement):
        previous = getattr(owner, name)
        self.patches.append((owner, name, previous))
        setattr(owner, name, replacement)
        return previous

    def install(self, q):
        from app.persistence.database import LocalSQLite, _TrackedConnection
        from app.analytics.scheduling import _OwnedBoundedExecutor
        from app.analytics.database import ProjectionsDatabase
        trace = self
        original_write = q.write_once
        def measured_write(path, value):
            start = time.monotonic()
            try:
                return original_write(path, value)
            finally:
                trace.add('journal_fsync', start, name=Path(path).name)
        self.patch(q, 'write_once', measured_write)
        original_tx = LocalSQLite.transaction
        @contextmanager
        def transaction(db, *args, **kwargs):
            start = time.monotonic()
            cpu = time.thread_time()
            phase = trace.phase
            opened = body_done = None
            try:
                with original_tx(db, *args, **kwargs) as connection:
                    opened = time.monotonic()
                    try:
                        yield connection
                    finally:
                        body_done = time.monotonic()
            finally:
                trace.add('transaction', start, store=db.path.name,
                          opened=opened, body_done=body_done, phase=phase,
                          thread_cpu_seconds=time.thread_time()-cpu)
        self.patch(LocalSQLite, 'transaction', transaction)
        original_close = _TrackedConnection.close
        def close(connection):
            start = time.monotonic()
            cpu = time.thread_time()
            phase = trace.phase
            try:
                return original_close(connection)
            finally:
                trace.add('connection_close', start, phase=phase,
                              thread_cpu_seconds=time.thread_time()-cpu)
        self.patch(_TrackedConnection, 'close', close)
        original_submit = _OwnedBoundedExecutor.submit
        def submit(executor, fn):
            queued = time.monotonic()
            phase = trace.phase
            label = getattr(getattr(fn, 'func', fn), '__qualname__', type(fn).__name__)
            def execute():
                start = time.monotonic()
                cpu = time.thread_time()
                try:
                    return fn()
                finally:
                    trace.add('owned_call', start, submitted=queued, name=label, phase=phase,
                              thread_cpu_seconds=time.thread_time()-cpu)
            return original_submit(executor, execute)
        self.patch(_OwnedBoundedExecutor, 'submit', submit)
        if hasattr(ProjectionsDatabase, 'passive_wal_checkpoint'):
            original = ProjectionsDatabase.passive_wal_checkpoint
            def checkpoint(db):
                start = time.monotonic()
                answer = None
                phase = trace.phase
                cpu = time.thread_time()
                try:
                    answer = original(db)
                    return answer
                finally:
                    trace.add('passive_checkpoint', start, frames=answer, phase=phase,
                              thread_cpu_seconds=time.thread_time()-cpu)
            self.patch(ProjectionsDatabase, 'passive_wal_checkpoint', checkpoint)

        original_executor = asyncio.BaseEventLoop.run_in_executor
        def run_in_executor(loop, executor, fn, *args):
            queued, phase = time.monotonic(), trace.phase
            label = getattr(getattr(fn, 'func', fn), '__qualname__', type(fn).__name__)
            def execute():
                started, cpu = time.monotonic(), time.thread_time()
                try:
                    return fn(*args)
                finally:
                    trace.add('asyncio_executor_call', started, phase=phase, name=label,
                              submitted=queued, thread_cpu_seconds=time.thread_time()-cpu)
            return original_executor(loop, executor, execute)
        self.patch(asyncio.BaseEventLoop, 'run_in_executor', run_in_executor)

    def restore(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)


def summarize_probe(probe):
    clocks = probe['clocks']
    required = ('durable_canonical_commit', 'first_valid_visible_result',
                'required_cleanup_complete', 'backlog_drained')
    if any(clocks.get(key) is None for key in required):
        raise ValueError('probe_clock_missing')
    if (not probe.get('valid_current_result') or not probe.get('cleanup_complete')
            or not probe.get('stale_reference_rejected') or probe.get('backlog_before') != 0
            or probe.get('backlog_after') != 0):
        raise ValueError('probe_safety_incomplete')
    times = {event['stage']: event['at'] for event in probe['publication_events']}
    committed = clocks['durable_canonical_commit']
    done = max(clocks[key] for key in required[1:])
    return dict(case=probe['case'], total=done-committed, gate=done-committed <= 10,
        commit_to_built=times['built']-committed,
        built_to_validated=times['validated']-times['built'],
        validated_to_done=done-times['validated'],
        activation_to_cleanup=clocks['required_cleanup_complete']-times['activated'],
        precommit=committed-clocks['operation_started'])


def bind_baseline(args, q, light, result):
    path = args.baseline_operation
    probe = q.read_json(path)['value']
    fixture = q.read_json(path.parent.parent/'fixture.json')
    process = q.read_json(path.parent.parent/'process.json')
    if (probe['case'] != 'ordinary/'+args.case or probe['source_before']['messages'] != args.messages
            or fixture['manifest_sha256'] != result['manifest_sha256']
            or fixture['kind_adapter'] != 'production_unknown_kinds'
            or q.digest(process['runtime']) != result['runtime_sha256']):
        raise ValueError('baseline_binding_mismatch')
    result['baseline'] = dict(path=str(path), sha256=light.file_sha256(path),
        summary=summarize_probe(probe), runtime_sha256=q.digest(process['runtime']))


def wal_sizes(workdir):
    # File metadata only: never issue a diagnostic checkpoint or read SQLite pages.
    answer = {}
    for name in ('canonical.sqlite3', 'projections.sqlite3', 'analytics.sqlite3'):
        for suffix in ('', '-wal', '-shm'):
            path = workdir/(name+suffix)
            try:
                answer[path.name] = path.stat().st_size
            except FileNotFoundError:
                answer[path.name] = None
    return answer


async def cold_prefix(work, journal, resources, scheduler, case):
    # Same helper calls and ordering as analytics_qualification_worker.visibility.
    # No ready-seed shortcut, extra prepare_questions, wait, idle, checkpoint or reopen.
    from tools.analytics_qualification_workloads import direct, scheduled
    cold = await direct(work, journal, resources, 'cold')
    resources.start()
    await scheduler.start(recover=True)
    probe = await scheduled(work, journal, resources, scheduler, 'one_committed_message',
        lambda: work.add(0 if case == 'dominant' else 1, 'visibility-ordinary-'+case),
        case='ordinary/'+case)
    return cold, probe


async def run_cold(args, q, light, trace, status, manifest, result, workdir):
    from tools.analytics_qualification_fixture import Workload, Journal
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    trace.phase = 'fixture'
    with light.PhaseHeartbeat(status, 'cold-fixture', interval=30):
        work = Workload(workdir, manifest, args.messages, reopen=False, known_kinds=False)
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1,
                                            queue_capacity=64, reconciliation_interval=30)
    result['safety'] = {}
    result['wal_boundaries'] = []
    journal = Journal(args.output/'events', str(os.getpid()))
    save = journal.save
    def observed_save(label, value):
        if label == 'operation-started':
            trace.phase = value.get('case') or value['phase']
            result['wal_boundaries'].append(dict(phase=trace.phase, at=time.monotonic(),
                                                sizes=wal_sizes(workdir)))
            light.atomic_status(status, trace.phase, state='started')
        saved = save(label, value)
        if label == 'operation':
            if value['phase'] == 'one_committed_message':
                summary = summarize_probe(value)
                result.update(probe=value, summary=summary)
                light.atomic_status(status, 'first-update-measured', **summary)
            trace.phase = value['phase']+'-independent-verification'
        if label == 'phase':
            light.atomic_status(status, value['phase']+'-verified',
                seconds=value['independent_verification_seconds'])
        return saved
    journal.save = observed_save
    try:
        result['cold'], result['probe'] = await cold_prefix(work, journal, resources, scheduler, args.case)
        result['summary'] = summarize_probe(result['probe'])
        result['complete'] = True
    finally:
        trace.phase = 'shutdown'
        resources.close()
        result['safety']['scheduler_closed'] = await scheduler.close(timeout=10)
        result['safety']['detached_workers'] = scheduler.detached_worker_count
        result['safety']['backlog'] = scheduler.retained_account_count
        result['wal_boundaries'].append(dict(phase='pre-close', at=time.monotonic(), sizes=wal_sizes(workdir)))
        work.close()
        if (not result['safety']['scheduler_closed'] or result['safety']['detached_workers']
                or result['safety']['backlog']):
            result['complete'] = False


PREFIX_CASES = ['ordinary/dominant', 'rebuilt/small', 'idle/dominant']


def prefix_outcome(result, messages):
    """A diagnostic completion is never a full visibility qualification pass."""
    if not result.get('complete'):
        return 'INCOMPLETE'
    if messages != 100000:
        return 'SMOKE_ONLY'
    rows = result.get('summaries', [])
    if [row['case'] for row in rows] != PREFIX_CASES:
        return 'EARLIER_PREFIX_GATE_FAILED'
    return 'NOT_REPRODUCED' if rows[-1]['gate'] else 'LATENCY_MISS_REPRODUCED'


async def repeat1_prefix(work, journal, config, instance):
    # Use the v7 worker itself. visibility() closes resources but never spawns
    # restart; collect_visibility() is deliberately not called by this diagnostic.
    from tools.analytics_qualification_worker import visibility
    return await visibility(work, journal, config, instance)


async def run_repeat1_prefix(args, q, light, trace, status, manifest, result, workdir):
    from uuid import uuid4
    from tools.analytics_qualification_fixture import Workload, Journal
    from tools.analytics_qualification_progress import CollectorProgress
    from tools.analytics_qualification_execution import mark_state, StateBudget
    cases = manifest['visibility']['process_cases'][1]
    if (cases != PREFIX_CASES + ['restarted/small']
            or manifest['visibility']['idle_seconds'] != 61
            or not manifest['visibility'].get('fail_fast_after_verified_probe')):
        raise ValueError('repeat1_v7_recipe_mismatch')
    schedule = dict(manifest['visibility_execution'], directory=str(args.output/'execution'))
    if args.timeout_seconds > schedule['maximum_worker_seconds']:
        raise ValueError('prefix_cannot_extend_worker_limit')
    Path(schedule['directory']).mkdir()
    config = dict(repeat=1, job='visibility/reference-windows-16g/1',
                  execution_schedule=schedule, continue_after_visibility_failure=False)
    instance = str(os.getpid()) + ':' + uuid4().hex
    journal = Journal(args.output/'events', instance)
    progress = CollectorProgress(args.output, instance)
    result.update(prefix_config=config, intentionally_unexecuted_cases=cases[-1:])
    begun = time.monotonic()
    budget = StateBudget(schedule, started=begun, token=os.environ.get('OFCA_QUALIFICATION_PROCESS'))
    stop = threading.Event()
    def watch():
        while not stop.wait(0.5):
            try:
                budget.poll(time.monotonic())
            except Exception as error:
                q.write_once(args.output/'state-aborted.json',
                             dict(complete=False, error_type=type(error).__name__, error=str(error)))
                os._exit(98)
    monitor = threading.Thread(target=watch, name='prefix-state-budget', daemon=True)
    mark_state(config, 'cold', instance)
    monitor.start()
    work = None
    try:
        with progress.phase('fixture.initialization'):
            work = Workload(workdir, manifest, args.messages, reopen=False, known_kinds=False)
        work.qualification_progress = progress
        q.write_once(args.output/'fixture.json', dict(definition=manifest['fixture'],
            manifest_sha256=q.digest(manifest), source_counts=work.counts(),
            kind_adapter='production_unknown_kinds', input_path='direct_synthetic_database_fixture'))
        if args.trace_mode != 'none':
            original_save = journal.save
            def save(label, value):
                if label == 'operation-started':
                    trace.phase = value.get('case') or value['phase']
                saved = original_save(label, value)
                if label == 'operation':
                    trace.phase = (value.get('case') or value['phase'])+'-verification'
                return saved
            journal.save = save
        report = await repeat1_prefix(work, journal, config, instance)
        result['prefix_report'] = report
        result['summaries'] = [summarize_probe(probe) for probe in report['probes']]
        result['probe_checks'] = {probe['case']: q.check_visibility_probe(
            manifest, 'reference-windows-16g', probe) for probe in report['probes']}
        if result['summaries']:
            result['summary'] = result['summaries'][-1]
        result['safety'] = {key: report.get(key) for key in
                            ('scheduler_closed', 'detached_workers', 'backlog')}
        verified = all(probe.get('independent_rebuild_equal') is True
                       and probe.get('persisted_content_revalidated') is True
                       for probe in report['probes'])
        orderly = (report.get('complete') is True or
                   report.get('stopped_after_verified_failure', {}).get('verification_completed') is True)
        result['complete'] = bool(report['probes']) and verified and orderly and (
            report.get('scheduler_closed') is True and report.get('detached_workers') == 0
            and report.get('backlog') == 0 and 'error' not in report)
    finally:
        stop.set()
        monitor.join(timeout=5)
        # Retain the full state schedule: restarted is intentionally absent.
        budget.poll(time.monotonic())
        result['execution'] = budget.report(time.monotonic())
        result['state_monitor_joined'] = not monitor.is_alive()
        if work is not None:
            work.close()
        if monitor.is_alive():
            result['complete'] = False


def main():
    args = options()
    import sys
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    from tools import targeted_visibility_benchmark as light
    q = light.qualification(root)
    source = light.validate_source(q, root, args.expected_sha, root.parent/'verification-keyring')
    if args.output.exists():
        raise FileExistsError(args.output)
    others = light._other_benchmark_jobs()
    if others:
        raise RuntimeError('competing_benchmark:'+str(others))
    args.output.mkdir(parents=True)
    status = args.output/'status.json'
    result = dict(schema='a07-light-first-update.v2', complete=False,
        source=source, source_revision=source['revision'],
        runner_sha256=light.file_sha256(Path(__file__)), trace_mode=args.trace_mode,
        diagnostic_only=True, qualifies_full_protocol=False,
        startup_mode='shared_verified_seed_candidate_currentness_then_first_update',
        limitations=['Does not replay cold construction or its physical WAL history.',
                     'Cross-source seed is explicit, not relabelled candidate seed.',
                     'Coarse attribution enabled; not frozen latency qualification.'])
    focused = args.preparation.startswith('focused-')
    trace = Trace()
    guard = None
    workdir = args.output/'work'
    begun = time.monotonic()
    with q.owner_lock(args.owner_lock):
        try:
            guard = light.MemoryGuard(job_limit=light.DEFAULT_JOB_MEMORY,
                host_reserve=light.DEFAULT_HOST_RESERVE,
                preflight_minimum=light.DEFAULT_PREFLIGHT)
            if shutil.disk_usage(args.output).free < 8*1024**3:
                raise RuntimeError('insufficient_disk_headroom')
            manifest = q.read_json(root/'docs/analytics/acceptance-manifest.json')
            runtime = q.runtime_context()
            result['manifest_sha256'] = q.digest(manifest)
            result['measurement_version'] = manifest['measurement']['version']
            result['app_source_sha256'] = q.digest({name: sha for name, sha in source['files'].items()
                                                   if name.startswith('app/')})
            result['runtime_sha256'] = q.digest(runtime)
            result['runtime'] = runtime
            result['helper_sha256'] = {name: light.file_sha256(root/'tools'/name)
                for name in ('targeted_visibility_benchmark.py',
                             'analytics_qualification_fixture.py',
                             'analytics_qualification_workloads.py',
                             'analytics_qualification_worker.py',
                             'analytics_qualification.py',
                             'analytics_qualification_execution.py',
                             'analytics_qualification_progress.py')}
            if args.preparation == 'ready':
                light.atomic_status(status, 'copy', state='started')
                seed = light.seed_metadata(q, args.seed)
                if seed['manifest_sha256'] != q.digest(manifest):
                    raise ValueError('seed_manifest_mismatch')
                result['seed'] = dict(path=str(args.seed), metadata_sha256=light.file_sha256(args.seed/'ready-seed.json'),
                    source_revision=seed['source_revision'], verification=seed['verification'],
                    counts=seed['counts'], database_sha256=seed['database_sha256'])
                result['copied_database_sha256'] = light.copy_databases(args.seed, workdir, expected=seed['database_sha256'])
            else:
                result.update(startup_mode='empty_database_cold_qualification_prefix',
                    preparation_recipe='a07-cold-first-update.v1', messages=args.messages,
                    limitations=['Source diagnostic, not full or packaged qualification.',
                                 'Coarse tracing may affect timing.',
                                 'One latency miss does not attribute its cause.'])
                if args.baseline_operation:
                    bind_baseline(args, q, light, result)
            if args.preparation == 'repeat1-prefix':
                result.update(schema='a07-light-first-update.v3',
                    preparation_recipe='a07-idle-dominant-prefix.v1',
                    startup_mode='fresh_repeat1_prefix_stop_before_restart',
                    repeat=1, profiling=args.trace_mode != 'none',
                    limitations=['Diagnostic prefix, not full or packaged qualification.',
                                 'One sample cannot establish causality or a speedup.'])
            if focused:
                result.update(schema='a07-focused-insertion.v1',
                    preparation_recipe=args.preparation+'.v1',
                    startup_mode='isolated_content_component' if args.preparation == 'focused-component'
                        else 'fresh_shortened_scheduled_fixture',
                    focused_repeats=args.focused_repeats, focused_operation=args.focused_operation,
                    profiling=args.trace_mode != 'none', qualifies_latency=False,
                    limitations=['Not the v7 lifecycle prefix or a qualification run.',
                        'Component mode excludes graph work, source-proof admission, scheduler, idle and disk waits.',
                        'Scheduled mode retains real validation and shutdown but omits the forced-rebuild/idle lifecycle.',
                        'Append versus insert is an operation-cost comparison, not a candidate speedup.',
                        'Nested spans overlap; use self_seconds or disjoint root spans.'])
                result['helper_sha256']['analytics_insertion_diagnostic.py'] = light.file_sha256(
                    root/'tools/analytics_insertion_diagnostic.py')
            q.write_once(args.output/'started.json', dict(result, complete=False))
            def expired():
                q.write_once(args.output/'aborted.json', dict(complete=False, reason='timeout',
                    seconds=args.timeout_seconds))
                os._exit(98)
            watchdog = threading.Timer(args.timeout_seconds, expired)
            watchdog.daemon = True
            watchdog.start()
            try:
                if args.trace_mode != 'none' and not focused:
                    trace.install(q)
                def pulse():
                    while not heartbeat_stop.wait(30):
                        light.atomic_status(status, trace.phase, state='running',
                            elapsed_seconds=round(time.monotonic()-begun, 3),
                            trace_events=len(trace.events), memory_guard=dict(guard.state))
                heartbeat_stop = threading.Event()
                heartbeat = threading.Thread(target=pulse, name='first-update-heartbeat', daemon=True)
                heartbeat.start()
                if focused:
                    from tools.analytics_insertion_diagnostic import run_component, run_update
                    entry = run_component if args.preparation == 'focused-component' else run_update
                else:
                    entry = {'cold': run_cold, 'ready': run, 'repeat1-prefix': run_repeat1_prefix}[args.preparation]
                asyncio.run(entry(args, q, light, trace, status, manifest, result, workdir))
            finally:
                watchdog.cancel()
                if 'heartbeat_stop' in locals():
                    heartbeat_stop.set()
                    heartbeat.join(timeout=5)
        except BaseException as error:
            result['complete'] = False
            result['error'] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
        finally:
            trace.restore()
            result['trace'] = trace.events
            result['memory_guard'] = guard.close() if guard else None
            result['source_unchanged'] = q.source_context(root) == source
            result['other_benchmarks_after'] = light._other_benchmark_jobs()
            result['wall_seconds'] = time.monotonic()-begun
            if not result['source_unchanged'] or result['other_benchmarks_after']:
                result['complete'] = False
            result['reproduction_status'] = ('INCOMPLETE' if not result['complete'] else
                'SMOKE_ONLY' if args.messages != 100000 else 'LATENCY_MISS_REPRODUCED' if args.preparation == 'cold' and args.messages == 100000
                and not result['summary']['gate'] else 'NOT_REPRODUCED')
            if args.preparation == 'repeat1-prefix':
                result['reproduction_status'] = prefix_outcome(result, args.messages)
            if focused:
                result['reproduction_status'] = 'FOCUSED_DIAGNOSTIC_COMPLETE' if result['complete'] else 'INCOMPLETE'
            if workdir.exists():
                shutil.rmtree(workdir)
            result['working_copy_removed'] = not workdir.exists()
            q.write_once(args.output/'result.json', result)
            sha = light.file_sha256(args.output/'result.json')
            light.atomic_status(status, 'finished', complete=result['complete'], receipt_sha256=sha)
            print(json.dumps({k:v for k,v in result.items() if k in ('source_revision','summary','summaries','reproduction_status','error','wall_seconds','source_unchanged','complete')}, sort_keys=True), flush=True)
    return 0 if result['complete'] else 2


async def run(args, q, light, trace, status, manifest, result, workdir):
    from tools.analytics_qualification_fixture import Workload, Journal
    from tools.analytics_qualification_workloads import observed_question, operation_record
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.analytics.errors import CanonicalRevisionChanged
    from app.models.analytics import AvailabilityStatus
    with light.verified_seed_storage_start():
        work = Workload(workdir, manifest, 100000, reopen=True, known_kinds=False)
        work.f.stores.projections.ensure_ready()
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1,
                                            queue_capacity=64, reconciliation_interval=30)
    result['safety'] = {}
    try:
        trace.phase = 'preparation'
        start = time.monotonic()
        with light.PhaseHeartbeat(status, 'currentness', interval=60):
            current = work.counts()['revision']
            prepared = await asyncio.to_thread(work.f.pipeline.prepare_questions, work.account, current)
            if not prepared:
                raise ValueError('candidate_seed_not_current')
            result['reference'] = await asyncio.to_thread(work.capture_current_reference)
        result['prepare_seconds'] = time.monotonic()-start
        resources.start()
        await scheduler.start(recover=True)
        state = await scheduler.wait(work.account)
        if state.availability != AvailabilityStatus.AVAILABLE:
            raise ValueError('scheduler_not_ready')
        if args.idle_seconds:
            with light.PhaseHeartbeat(status, 'idle', interval=60):
                await asyncio.sleep(args.idle_seconds)
        before = work.counts()
        previous = work.last
        work.observe_activation()
        journal = Journal(args.output/'events', str(os.getpid()))
        case = 'ordinary/'+args.case
        trace.phase = case
        start = time.monotonic()
        journal.save('operation-started', dict(case=case, at=start, source_before=before))
        committed = await asyncio.to_thread(work.add, 0 if args.case=='dominant' else 1,
                                             'light-first-update-'+args.case)
        journal.save('canonical-commit', dict(at=committed, source_after=work.counts()))
        stale = False
        try:
            await asyncio.to_thread(work.f.stores.projections.read_generation_artifact, work.account, previous)
        except CanonicalRevisionChanged:
            stale = True
        if not stale:
            raise ValueError('stale_reference_readable')
        after = work.counts()
        await scheduler.schedule(work.account, after['revision'])
        observations = []
        outcomes = set()
        async def visible():
            limit = time.monotonic()+120
            while time.monotonic()<limit:
                observation = await asyncio.to_thread(observed_question, work, resources)
                observations.append(observation)
                outcome = (observation['current'], observation['error'])
                if outcome not in outcomes:
                    journal.save('visibility-observation', observation)
                    outcomes.add(outcome)
                if observation['current']:
                    return observation['at']
                await asyncio.sleep(manifest['measurement']['visibility_poll_seconds'])
            raise TimeoutError('visibility_probe_timeout')
        observer = asyncio.create_task(visible())
        try:
            state = await asyncio.wait_for(scheduler.wait(work.account), timeout=120)
            drained = time.monotonic()
            journal.save('backlog-drained', dict(at=drained))
            seen = await observer
        finally:
            if not observer.done():
                observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        cleanup = work.cleaned.get(work.last.generation_id)
        result['probe'] = operation_record(work, 'one_committed_message', before,
            start, committed, seen, drained, case=case, valid_current_result=seen is not None,
            cleanup_complete=cleanup is not None, stale_reference_rejected=stale,
            backlog_before=0, backlog_after=scheduler.retained_account_count,
            availability_observations=observations)
        times = {e['stage']:e['at'] for e in result['probe']['publication_events']}
        done = max(seen, cleanup, drained)
        result['summary'] = dict(case=case, total=done-committed,
            gate=done-committed <= 10,
            commit_to_built=times['built']-committed,
            built_to_validated=times['validated']-times['built'],
            validated_to_done=done-times['validated'],
            activation_to_cleanup=cleanup-times['activated'],
            precommit=committed-start)
        journal.save('operation', result['probe'])
        if state.availability != AvailabilityStatus.AVAILABLE or state.attempted_revision!=after['revision'] or scheduler.retained_account_count:
            raise ValueError('publication_incomplete')
        trace.phase = 'post-probe'
        if args.verify_after:
            with light.PhaseHeartbeat(status, 'independent-rebuild', interval=60):
                result['independent_verification'] = await asyncio.to_thread(work.verify)
        result['complete'] = True
    finally:
        trace.phase = 'shutdown'
        resources.close()
        result['safety']['scheduler_closed'] = await scheduler.close(timeout=10)
        result['safety']['detached_workers'] = scheduler.detached_worker_count
        result['safety']['backlog'] = scheduler.retained_account_count
        work.close()
        if not result['safety']['scheduler_closed'] or result['safety']['detached_workers']:
            result['complete'] = False


if __name__ == '__main__':
    raise SystemExit(main())
