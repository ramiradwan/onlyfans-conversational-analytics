"""Opt-in update attribution around the unchanged v7 prefix worker."""
from functools import wraps
from pathlib import Path
import threading
import time

from tools.analytics_update_attribution import UpdateAttribution, MAX_SPANS, MAX_SQL_GROUPS

CASES = ('ordinary/dominant', 'rebuilt/small', 'idle/dominant')
MAX_EXECUTOR_CALLS = 4096


class ExecutorCalls:
    """Sparse ownership envelopes, including calls that start before a probe."""
    def __init__(self, phase):
        self.phase = phase
        self.events = []
        self.lock = threading.Lock()
        self.patches = []

    def wrap(self, kind, fn, args=()):
        label = getattr(getattr(fn, 'func', fn), '__qualname__', type(fn).__name__)
        with self.lock:
            if len(self.events) >= MAX_EXECUTOR_CALLS:
                raise RuntimeError('prefix_executor_trace_capacity_exceeded')
            item = dict(kind=kind, name=label, phase=self.phase(), submitted=time.monotonic(),
                        start=None, end=None, thread=None)
            self.events.append(item)
        def execute():
            with self.lock:
                item.update(start=time.monotonic(), thread=threading.get_ident(),
                            phase_at_start=self.phase())
            cpu = time.thread_time()
            try:
                return fn(*args)
            except BaseException as error:
                with self.lock: item['error_type'] = type(error).__name__
                raise
            finally:
                with self.lock:
                    item.update(end=time.monotonic(), thread_cpu_seconds=time.thread_time()-cpu)
        return item, execute

    def patch(self, owner, name, value):
        self.patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def install(self):
        import asyncio
        from app.analytics.scheduling import _OwnedBoundedExecutor
        original = _OwnedBoundedExecutor.submit
        def submit(executor, fn):
            item, wrapped = self.wrap('owned_call', fn)
            try: return original(executor, wrapped)
            except BaseException as error:
                with self.lock: item.update(end=time.monotonic(), submission_error=type(error).__name__)
                raise
        self.patch(_OwnedBoundedExecutor, 'submit', submit)
        original_async = asyncio.BaseEventLoop.run_in_executor
        def run_in_executor(loop, executor, fn, *args):
            item, wrapped = self.wrap('asyncio_executor_call', fn, args)
            try: return original_async(loop, executor, wrapped)
            except BaseException as error:
                with self.lock: item.update(end=time.monotonic(), submission_error=type(error).__name__)
                raise
        self.patch(asyncio.BaseEventLoop, 'run_in_executor', run_in_executor)

    def snapshot(self):
        with self.lock: return [dict(event) for event in self.events]

    def restore(self):
        for owner, name, previous in reversed(self.patches):
            setattr(owner, name, previous)
        self.patches.clear()


class PrefixAttribution:
    def __init__(self, journal, q, output, *, worker=None):
        if worker is None:
            from tools import analytics_qualification_worker as worker
        self.worker, self.journal, self.q, self.output = worker, journal, q, Path(output)
        self.phase = 'lifecycle'
        self.trace = UpdateAttribution(enabled=False, max_events=3*MAX_SPANS,
                                       max_sql_groups=3*MAX_SQL_GROUPS)
        self.calls = ExecutorCalls(lambda: self.phase)
        self.windows = []
        self.previous = []
        self.current = None
    def __enter__(self):
        try:
            self.trace.install()
            from app.analytics import graph_membership_selection as selection
            from app.analytics import conversation_id_frames as frames
            from app.analytics import conversation_graph_insertion as insertion
            from app.analytics.pipeline import AnalyticsPipeline
            from app.analytics.database import ProjectionsDatabase
            self.trace.patch(selection, 'prepare_selection', after=lambda value: {} if value is None
                             else dict(selected_rows=value.rows, selected_bytes=value.bytes))
            self.trace.patch(selection.MembershipSelection, 'discard',
                             lambda a,k: dict(released_rows=a[0].rows, released_bytes=a[0].bytes))
            self.trace.patch(frames, 'canonical_groups')
            self.trace.patch(frames, 'checked_predecessor_groups')
            self.trace.patch(frames.IdGroups, 'pack_contract', lambda a,k: dict(records=len(a[0])))
            self.trace.patch(insertion, 'create_membership_unit')
            self.trace.patch(AnalyticsPipeline, '_source_identity_matches')
            self.trace.patch(ProjectionsDatabase, 'passive_wal_checkpoint')
            self.calls.install()
            self.previous = [(self.worker, 'scheduled', self.worker.scheduled),
                             (self.journal, 'save', self.journal.save)]
            scheduled = self.worker.scheduled
            @wraps(scheduled)
            async def measured(*args, **kwargs):
                case = kwargs.get('case')
                if case not in CASES or case in [w['case'] for w in self.windows]:
                    raise ValueError('prefix_attribution_case_invalid')
                self.phase = self.trace.phase = case
                self.current = dict(case=case, enabled_at=time.monotonic(),
                                    executor_calls_at_entry=self.calls.snapshot())
                self.windows.append(self.current)
                self.trace.enabled = True
                try:
                    return await scheduled(*args, **kwargs)
                finally:
                    self.trace.enabled = False
                    self.current['scheduled_returned_at'] = time.monotonic()
                    self.current = None
                    self.phase = 'lifecycle'
            self.worker.scheduled = measured
            saved = self.journal.save
            def save(label, value):
                result = saved(label, value)
                if label == 'operation' and value.get('case') in CASES:
                    self.trace.enabled = False
                    self.phase = 'independent-verification'
                    self.current['operation_recorded_at'] = time.monotonic()
                    name = value['case'].replace('/', '-')
                    self.q.write_once(self.output/'attribution'/(name+'.json'), dict(
                        probe=value, at=self.q.stamp(), independent_verification_pending=True,
                        full_attribution=self.trace.snapshot(), executor_calls=self.calls.snapshot()))
                if label == 'phase' and value.get('case') in CASES:
                    self.current['independent_verification_completed'] = bool(
                        value.get('independent_rebuild_equal') and value.get('persisted_content_revalidated'))
                return result
            self.journal.save = save
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        self.trace.enabled = False
        for owner, name, previous in reversed(self.previous):
            setattr(owner, name, previous)
        self.previous.clear()
        try:
            self.calls.restore()
        finally:
            self.trace.restore()

    def snapshot(self):
        return dict(schema='a07-repeat1-update-attribution.v1', windows=self.windows,
                    full_attribution=self.trace.snapshot(), executor_calls=self.calls.snapshot(),
                    maximum_executor_calls=MAX_EXECUTOR_CALLS,
                    limits_note='Three update windows; three times the original per-update trace capacity.',
                    limitation='Cold and oracle content calls are not traced. Executor envelopes include overlapping maintenance; snapshots before the oracle cannot prove equality or shutdown.')
