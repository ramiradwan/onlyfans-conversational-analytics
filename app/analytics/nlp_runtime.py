"""Explicit ownership of optional background findings and publication."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from functools import partial
import threading
import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.text_analysis_selection import FixtureTextAnalysisExecutionConfiguration
    from app.services.text_analysis_execution import LicensedFindingsBinding
    from app.services.text_analysis_preview_selection import FixtureTextAnalysisPreviewConfiguration

from app.analytics.errors import ProjectionBuildCancelled, ProjectionCoordinatorClosed, ProjectionStorageUnavailable
from app.analytics.nlp_findings_store import FindingsStore, MAX_ACCOUNTS
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.nlp_execution import require_findings_execution
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionCancelled
from app.analytics.scheduling import _OwnedBoundedExecutor
from app.nlp.profiles import read_profile
from app.services.nlp_dependencies import dependency_service, configure_dependency_service

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FindingsConfiguration:
    dependencies: Callable
    authorize: Callable
    clock: Callable = lambda: datetime.now(timezone.utc)
    label_resolver: Callable | None = None
    interval: float = .25
    execution: FixtureTextAnalysisExecutionConfiguration | None = None
    preview: FixtureTextAnalysisPreviewConfiguration | None = None

    def __post_init__(self):
        if not all(callable(value) for value in (self.dependencies, self.authorize, self.clock)):
            raise ValueError('findings_configuration_invalid')
        if type(self.interval) not in (int, float) or not .01 <= self.interval <= 60:
            raise ValueError('findings_interval_invalid')
        if self.execution is not None:
            from app.services.text_analysis_selection import FixtureTextAnalysisExecutionConfiguration
            if type(self.execution) is not FixtureTextAnalysisExecutionConfiguration:
                raise ValueError('findings_execution_configuration_invalid')
        if self.preview is not None:
            from app.services.text_analysis_preview_selection import FixtureTextAnalysisPreviewConfiguration
            if type(self.preview) is not FixtureTextAnalysisPreviewConfiguration:
                raise ValueError('findings_preview_configuration_invalid')
        if self.preview is not None and self.execution is not None:
            if self.preview.authentication is not self.execution.authentication:
                raise ValueError('findings_authority_owner_mismatch')


class _BoundDatabase:
    def __init__(self, owner, store):
        self.owner, self.store = owner, store
        self.gate = None

    @contextmanager
    def lease(self):
        with (self.gate if self.gate is not None else nullcontext()), self.owner.lifecycle_store() as current:
            if current is not self.store:
                raise ProjectionStorageUnavailable()
            yield current

    @contextmanager
    def read(self):
        with self.lease() as current, current.database.read() as connection:
            yield connection

    @contextmanager
    def transaction(self, **kwargs):
        with self.lease() as current, current.database.transaction(**kwargs) as connection:
            yield connection

    def bind_contribution_origins(self, *args):
        with self.lease() as current:
            current.database.bind_contribution_origins(*args)


@dataclass
class _EnrollmentRunner:
    key: tuple
    binding: LicensedFindingsBinding
    runner: FindingsRunner
    after_job: str = ''


class _BoundProjections:
    def __init__(self, owner, store):
        self.database = _BoundDatabase(owner, store)
        self._graph = _BoundGraph(self.database)

    def __getattr__(self, name):
        with self.database.lease() as store:
            value = getattr(store, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            with self.database.lease() as store:
                return getattr(store, name)(*args, **kwargs)
        return call

    @property
    def graph(self):
        return self._graph

    def _call(self, name, *args, **kwargs):
        return self.__getattr__(name)(*args, **kwargs)

    def get(self, *a, **k): return self._call('get', *a, **k)
    def replace(self, *a, **k): return self._call('replace', *a, **k)
    def clear(self, *a, **k): return self._call('clear', *a, **k)
    def get_artifact(self, *a, **k): return self._call('get_artifact', *a, **k)
    def replace_artifact(self, *a, **k): return self._call('replace_artifact', *a, **k)
    def stage_artifact(self, *a, **k): return self._call('stage_artifact', *a, **k)
    def publish_generation(self, *a, **k): return self._call('publish_generation', *a, **k)
    def discard_generation(self, *a, **k): return self._call('discard_generation', *a, **k)
    def next_projection_generation(self, *a, **k): return self._call('next_projection_generation', *a, **k)
    def open_publication_epoch(self, *a, **k): return self._call('open_publication_epoch', *a, **k)
    def revoke_publication_epoch(self, *a, **k): return self._call('revoke_publication_epoch', *a, **k)
    def fence_publication_epoch(self, *a, **k): return self._call('fence_publication_epoch', *a, **k)


class _BoundGraph:
    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        def call(*args, **kwargs):
            with self.database.lease() as store:
                return getattr(store.graph, name)(*args, **kwargs)
        return call


class _GatedPipeline:
    def __init__(self, base, gate):
        self.base, self.gate = base, gate

    def __getattr__(self, name):
        return getattr(self.base, name)

    def prepare_questions(self, *args, **kwargs):
        with self.gate:
            return self.base.prepare_questions(*args, **kwargs)

    def build_candidate(self, *args, **kwargs):
        with self.gate:
            return self.base.build_candidate(*args, **kwargs)

    def publish_candidate(self, *args, **kwargs):
        with self.gate:
            return self.base.publish_candidate(*args, **kwargs)


class _AcknowledgedPipeline:
    def __init__(self, base, runner, account, signal, published, current_signal):
        self.base, self.runner, self.account = base, runner, account
        self.signal, self.published = signal, published
        self.current_signal = current_signal

    def __getattr__(self, name):
        return getattr(self.base, name)

    def prepare_questions(self, account, *args, **kwargs):
        with self.runner.dependencies.gate:
            self.runner.authorize(account)
            return self.base.prepare_questions(account, *args, **kwargs)

    def build_candidate(self, account, *args, **kwargs):
        with self.runner.dependencies.gate:
            self.runner.authorize(account)
            return self.base.build_candidate(account, *args, **kwargs)

    def publish_candidate(self, candidate):
        with self.runner.dependencies.gate:
            self.runner.authorize(self.account)
            if self.current_signal() != self.signal:
                raise ProjectionBuildCancelled()
            result = self.base.publish_candidate(candidate)
            self.runner.findings.acknowledge(self.account, self.signal[-1])
            with self.runner.findings.database.transaction() as db:
                current = db.execute('SELECT selection_epoch,admission_epoch,mode,notification_revision '
                    'FROM nlp_accounts WHERE account_ref=?', (account_ref(self.account),)).fetchone()
                if current is not None and tuple(current) == (*self.signal[:3], self.signal[-1]):
                    self.published[self.account] = self.signal
                    self.published.move_to_end(self.account)
                    while len(self.published) > MAX_ACCOUNTS:
                        self.published.popitem(last=False)
            return result


class FindingsRuntime:
    def __init__(self, source, pipeline, configuration, authorize):
        require_findings_execution()
        self.source, self.pipeline = source, pipeline
        self.configuration, self.authorize_account = configuration, authorize
        self._stop = threading.Event()
        self._wake = asyncio.Event()
        self._executor = _OwnedBoundedExecutor(max_workers=1, max_tasks=1,
            thread_name_prefix='analytics-findings')
        self._task = None
        self._future = None
        self._closing = None
        self._service = None
        self._store = None
        self._runner = None
        authority = configuration.execution or configuration.preview
        self._gate = (authority.authentication.database.dependency_authority_gate if authority is not None else None)
        self._baseline = _GatedPipeline(pipeline, self._gate) if self._gate is not None else pipeline
        self._execution_service = None
        self._enrollments = OrderedDict()
        self._locator_page = deque()
        self._after_account = ''
        self._preview_service = None
        self._preview_owner = None
        self._preview_page = deque()
        self._after_preview = ''
        self._prefer_preview = True
        self._accounts = deque(maxlen=MAX_ACCOUNTS)
        self._published = OrderedDict()
        self._preparation_attempts = set()
        self._after = None
        self._failed = False
        self._joined = False
        self.failure = None

    def start(self, scheduler):
        if self._task is None and not self._stop.is_set():
            self._task = asyncio.create_task(self._run(scheduler), name='analytics-findings')

    def wake(self):
        self._wake.set()

    def begin_stop(self):
        self._stop.set()
        self._wake.set()

    def _authorize(self, account, bound):
        self.authorize_account(account)
        if self._stop.is_set():
            raise QuestionCancelled()
        with bound.database.lease():
            pass

    def _retire(self):
        if self._service is not None:
            if not self._service.close(0):
                return False
        self._clear_binding()
        return True

    def _clear_binding(self):
        from app.services.text_analysis_execution import execution_service, configure_execution_service
        if self._execution_service is not None:
            if execution_service() is self._execution_service:
                configure_execution_service(None)
            if getattr(self._service, 'execution_owner', None) is self._execution_service:
                self._service.execution_owner = None
        if self._service is not None and dependency_service() is self._service:
            configure_dependency_service(None)
        if self._preview_service is not None:
            from app.services.text_analysis_preview import preview_service, configure_preview_service
            if preview_service() is self._preview_service:
                configure_preview_service(None)
        self._preview_service = self._preview_owner = None
        self._preview_page.clear()
        self._after_preview = ''
        self._service, self._runner, self._store = None, None, None
        self._accounts.clear()
        self._published.clear()
        self._preparation_attempts.clear()
        self._after = None
        self._execution_service = None
        self._enrollments.clear()
        self._locator_page.clear()
        self._after_account = ''

    def _bind(self):
        with self.pipeline.projections.lifecycle_store() as store:
            if store is None:
                return False
        if store is self._store:
            return not self._failed
        if not self._retire() or self._stop.is_set():
            return False
        if dependency_service() is not None:
            raise ValueError('findings_dependency_owner_conflict')
        bound = _BoundProjections(self.pipeline.projections, store)
        findings = FindingsStore(bound.database)
        self._store = store
        self._failed = True
        service = self.configuration.dependencies(self.source, findings)
        self._service = service
        if service.source is not self.source or service.findings is not findings:
            raise ValueError('findings_dependency_binding_invalid')
        bound.database.gate = service.gate
        if self._gate is not None and service.gate is not self._gate:
            raise ValueError('findings_execution_gate_mismatch')
        if self.configuration.execution is not None:
            from app.services.text_analysis_execution import execution_service
            if execution_service() is not None:
                raise ValueError('findings_execution_owner_conflict')
            self._execution_service = self.configuration.execution.bind(self.source, service)
        if self.configuration.preview is not None:
            from app.services.text_analysis_preview import preview_service
            from app.analytics.nlp_preview_execution import PreviewComputeOwner
            if preview_service() is not None:
                raise ValueError('findings_preview_owner_conflict')
            self._preview_service = self.configuration.preview.bind(self.source, service)
            self._preview_owner = PreviewComputeOwner(self._preview_service, service, self.source,
                clock=self.configuration.clock, cancellation_check=self._stop.is_set)
        service.start()
        configure_dependency_service(service)
        self._runner = FindingsRunner(self.source, bound,
            authorize=lambda account: self._authorize(account, bound),
            execution_ready=service.ready, adapter=None, dependencies=service,
            clock=self.configuration.clock, enrichment=self.pipeline.enrichment,
            label_resolver=self.configuration.label_resolver,
            cancellation_check=self._stop.is_set)
        if self.configuration.execution is not None:
            from app.services.text_analysis_execution import configure_execution_service
            configure_execution_service(self._execution_service)
        if self._preview_service is not None:
            from app.services.text_analysis_preview import configure_preview_service
            configure_preview_service(self._preview_service)
        self._failed = False
        self.failure = None
        return True

    def generation_pipeline(self, account):
        gate=self._gate or (self._service.gate if self._service is not None else nullcontext())
        with gate:
            return self._generation_pipeline(account)

    def _generation_pipeline(self, account):
        if self._stop.is_set():
            raise ProjectionCoordinatorClosed()
        runner = self._runner
        if self._execution_service is not None:
            entry = self._enrollments.get(account)
            if entry is None:
                return self._baseline
            runner = entry.runner
        if runner is None or self._failed:
            return self._baseline
        try:
            state = runner.findings.state(account)
        except (ValueError, ProjectionStorageUnavailable):
            return self._baseline
        # Authorization is independent of dependency readiness.
        runner.authorize(account)
        profile = read_profile(state['profile_json'])
        ready = True if self._execution_service is not None else runner.dependencies.ready(profile)
        signal = self._signal(state, ready)
        retained = self._execution_service is not None and state['mode'] in ('enabled', 'paused')
        base = runner.frozen_pipeline(account) if retained or (state['mode'] == 'enabled' and ready) else self.pipeline
        if retained and not base.retained_anchor:
            base = self.pipeline
        def current_signal():
            current = runner.findings.state(account)
            ready = True if self._execution_service is not None else runner.dependencies.ready(read_profile(current['profile_json']))
            return self._signal(current, ready)
        return _AcknowledgedPipeline(base, runner, account, signal, self._published, current_signal)

    @staticmethod
    def _signal(state, ready):
        return (state['selection_epoch'], state['admission_epoch'], state['mode'], ready,
                state['notification_revision'])

    def _turn(self):
        if self._stop.is_set() or not self._bind():
            return None
        runner = self._runner
        if self._preview_service is not None:
            preview_first = self._prefer_preview
            self._prefer_preview = not preview_first
            if preview_first and self._preview_turn():
                return None
            if not preview_first:
                return self._enrollment_turn() if self._execution_service is not None else self._legacy_turn()
        if self._execution_service is not None:
            return self._enrollment_turn()
        return self._legacy_turn()

    def _preview_turn(self):
        if not self._preview_page:
            page = self._preview_service.pending_locators(after_job=self._after_preview, limit=64)
            self._after_preview = page[-1].job_id if len(page) == 64 else ''
            self._preview_page.extend(page)
        if not self._preview_page:
            return False
        started = time.monotonic()
        binding = self._preview_service.bind_pending(self._preview_page.popleft())
        self._preview_owner.run(binding, started_at=started)
        return True

    def _legacy_turn(self):
        runner = self._runner
        with runner.dependencies.gate:
            selections = runner.findings.selected_profiles()
            selected = {row['account_ref'] for row in selections}
            for account in tuple(self._published):
                if account_ref(account) not in selected:
                    self._published.pop(account, None)
        page = self.source.account_revision_page(after=self._after, limit=64)
        self._after = page[-1][0] if len(page) == 64 else None
        for account, _ in page:
            if account_ref(account) in selected and account not in self._accounts:
                self._accounts.append(account)
        self._accounts = deque((account for account in self._accounts
            if account_ref(account) in selected), maxlen=MAX_ACCOUNTS)
        if not self._accounts:
            return None
        account = self._accounts[0]
        self._accounts.rotate(-1)
        runner.authorize(account)
        if not self.source.account_exists(account):
            self._accounts.remove(account)
            return None
        if runner.findings.state(account)['mode'] == 'enabled':
            state = runner.findings.state(account)
            profile = read_profile(state['profile_json'])
            digest = profile.identity_digest()
            if (not runner.dependencies.ready(profile) and digest not in self._preparation_attempts
                    and len(self._preparation_attempts) < 64):
                self._preparation_attempts.add(digest)
                def cancelled():
                    runner.authorize(account)
                    current = runner.findings.state(account)
                    return (current['mode'] != 'enabled'
                        or current['selection_epoch'] != state['selection_epoch']
                        or current['admission_epoch'] != state['admission_epoch']
                        or read_profile(current['profile_json']).identity_digest() != digest)
                try:
                    runner.dependencies.prepare_installed(digest, cancellation_check=cancelled)
                except Exception:
                    runner.authorize(account)
                    self.failure = 'findings_preparation_unavailable'
            runner.process_one(account)
        state = runner.findings.state(account)
        ready = runner.dependencies.ready(read_profile(state['profile_json']))
        if state['notification_revision'] > 0 and (
                state['acknowledged_revision'] != state['notification_revision']
                or self._published.get(account) != self._signal(state, ready)):
            from app.analytics.identity import source_identity
            return account, source_identity(self.source, account).revision, state['notification_revision']
        return None

    def _authorize_enrollment(self, entry, account):
        if self._stop.is_set():
            raise QuestionCancelled()
        self._execution_service.authorize_binding(entry.binding, account)
        with entry.runner.projections.database.lease():
            pass

    def _enrollment_turn(self):
        service = self._execution_service
        if not self._locator_page:
            page = service.current_locators(after_account=self._after_account, limit=64)
            self._after_account = page[-1].creator_account_id if len(page) == 64 else ''
            self._locator_page.extend(page)
        if not self._locator_page:
            return None
        locator = self._locator_page.popleft()
        binding = service.bind_current(locator)
        account = locator.creator_account_id
        if not self.source.account_exists(account):
            self._enrollments.pop(account, None)
            return None
        key = (locator.enrollment_id, locator.mode_version, binding.capsule.capsule_sha256)
        entry = self._enrollments.get(account)
        if entry is None or entry.key != key:
            runner = FindingsRunner(self.source, self._runner.projections, authorize=partial(service.authorize_binding, binding),
                execution_ready=self._service.ready, adapter=None, dependencies=self._service,
                clock=self.configuration.clock, enrichment=self.pipeline.enrichment,
                label_resolver=self.configuration.label_resolver, cancellation_check=self._stop.is_set,
                canonical_execution=binding.execution, execution_service=service, execution_binding=binding)
            entry = _EnrollmentRunner(key, binding, runner)
            runner.authorize = lambda account: self._authorize_enrollment(entry, account)
            self._enrollments[account] = entry
        else:
            entry.binding = binding
            entry.runner.execution_binding = binding
            entry.runner.canonical_execution = binding.execution
        self._enrollments.move_to_end(account)
        while len(self._enrollments) > MAX_ACCOUNTS:
            removed, _ = self._enrollments.popitem(last=False)
            self._published.pop(removed, None)
        runner = entry.runner
        runner.authorize(account)
        runner.reconcile(account, maximum=64)
        with service.retained_visibility(binding):
            with service.repository.database.read() as db:
                children = binding.execution.jobs.list_children_in(db, locator.enrollment_id,
                    policy=binding.identity_policy, after_id=entry.after_job, limit=64)
            for child in children:
                runner._budget().check()
                if child.state == 'committed':
                    from app.analytics.nlp_contribution_replay import replay_contribution
                    replay_contribution(service.repository, runner.findings, child.contribution_id,
                                        policy=binding.identity_policy, limit=1)
            entry.after_job = children[-1].job_id if len(children) == 64 else ''
        if binding.current.state == 'enabled':
            try:
                digest = binding.profile.identity_digest()
                if (not self._service.ready(binding.profile) and digest not in self._preparation_attempts
                        and len(self._preparation_attempts) < 64):
                    self._preparation_attempts.add(digest)
                    def cancelled():
                        runner.authorize(account)
                        with binding.execution._fence('prepare'):
                            pass
                        return self._stop.is_set()
                    self._service.prepare_installed(digest, cancellation_check=cancelled)
                runner.process_one(account)
            except ValueError:
                runner.authorize(account)
                with service.retained_visibility(binding):
                    pass
                self.failure = 'findings_execution_unavailable'
        state = runner.findings.state(account)
        ready = True
        if state['notification_revision'] > 0 and (
                state['acknowledged_revision'] != state['notification_revision']
                or self._published.get(account) != self._signal(state, ready)):
            from app.analytics.identity import source_identity
            return account, source_identity(self.source, account).revision, state['notification_revision']
        return None

    async def _run(self, scheduler):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._future = self._executor.submit(self._turn)
                notice = await asyncio.shield(asyncio.wrap_future(self._future))
                if notice is not None and not self._stop.is_set():
                    await scheduler.request_findings(*notice)
            except Exception:
                if self.failure is None:
                    LOGGER.warning('analytics_findings_event reason_code=processing_unavailable')
                self.failure = 'findings_unavailable'
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), self.configuration.interval)
                except TimeoutError:
                    pass

    def abort(self):
        self.begin_stop()
        if self._task is not None and not self._task.done():
            return False
        return self._finish_close(0)

    def _finish_close(self, timeout):
        if self._joined:
            return True
        if self._future is not None and not self._future.done():
            return False
        if self._service is None and self._closing is None:
            self._executor.begin_shutdown()
            self._executor.join()
            self._joined = True
            return True
        if self._closing is None:
            self._closing = self._executor.submit(lambda: self._retire_with_timeout(timeout))
        if not self._closing.done():
            return False
        try:
            drained = self._closing.result()
        except Exception:
            drained = False
        self._closing = None
        if not drained:
            return False
        self._executor.begin_shutdown()
        self._executor.join()
        self._joined = True
        return True

    def _retire_with_timeout(self, timeout):
        if self._service is not None and not self._service.close(timeout):
            return False
        self._clear_binding()
        return True

    async def close(self, timeout):
        self.begin_stop()
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if (self._task is None or self._task.done()) and self._finish_close(
                    max(0, deadline - time.monotonic())):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(.01, max(0, deadline-time.monotonic())))
