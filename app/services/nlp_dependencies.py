"""Explicit dependency lifecycle; absent release inputs always remain unavailable."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import copy
import json
import os
from pathlib import Path
import shutil
import re
import threading
import time
from uuid import uuid4

from app.nlp import adapter
from app.nlp.artifact_store import DATA_ROLES, ArtifactStoreError
from app.nlp.profile_authentication import authenticate_profile, MAX_ENVELOPE_BYTES
from app.nlp.profiles import ResourcePolicy, canonical_bytes, validate_resource_policy, read_profile
from app.nlp.release_catalog import (verify_catalog, canonical, require, DependencyRejected,
                                    CATALOG_ENVELOPE_BYTES, MAX_WITHDRAWALS)
from app.persistence.private_files import (reject_path_aliases, apply_private_directory_security,
    apply_private_file_security, private_file_identity, sync_directory)
from app.services.nlp_distribution import ACQUISITION_SECONDS


class ProcessOwners:
    """Observe an exact process lifetime; inaccessible observations fail closed."""
    def identity(self, pid):
        if os.name != 'nt':
            try:
                body = Path('/proc') / str(pid) / 'stat'
                fields = body.read_text().rsplit(')', 1)[1].split()
                return {'pid': pid, 'birth': fields[19]}
            except FileNotFoundError:
                return None
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise DependencyRejected('nlp_owner_observation')
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            require(kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)), 'nlp_owner_observation')
            birth = values[0].dwLowDateTime | values[0].dwHighDateTime << 32
            return {'pid': pid, 'birth': str(birth)}
        finally:
            kernel.CloseHandle(handle)

    def absent(self, owner):
        return self.identity(owner['pid']) != owner


class DependencyService:
    """One Brain owner serializes acquisition and short authority commits.

    Approval and engine providers are release-owned constructor dependencies.
    Neither signatures, HTTP requests nor database rows can provide them.
    """
    def __init__(self, persistence, artifacts, findings, source, distribution, *, keys,
                 approvals, engine_provider, resource_policy, spool_root,
                 clock=time.time, monotonic=time.monotonic, owners=None):
        require(type(resource_policy) is ResourcePolicy)
        self.policy = ResourcePolicy.model_validate_json(resource_policy.model_dump_json())
        require(artifacts.limits.maximum_store_bytes <= self.policy.installed_bytes, 'nlp_installed_limit')
        self.persistence, self.artifacts, self.findings, self.source = persistence, artifacts, findings, source
        self.distribution, self.keys, self.approvals, self.engine = distribution, copy.deepcopy(keys), approvals, engine_provider
        self.clock, self.monotonic, self.owners = clock, monotonic, owners or ProcessOwners()
        self.spool = reject_path_aliases(Path(spool_root))
        require(self.spool.is_absolute(), 'nlp_spool_root')
        self.gate = threading.RLock()
        self.thread = None
        self.stopping = threading.Event()
        self.available = {}
        self.catalog = None
        self.failure = None
        self.owner = None
        self.clock_floor = 0
        self._started = False
        self.blocked = False
        self.cached_profiles = []
        self.withdrawals = set()
        self.active = threading.Condition(self.gate)
        self.pins = {}

    def disable(self):
        """Isolate optional startup failure without modifying durable authority."""
        with self.gate:
            self.blocked = True
            self.failure = 'nlp_dependency_unavailable'
            self.available.clear()
        return self.close()

    def start(self):
        # Startup revalidates disk only. No HTTP, acquisition, warmup or analysis.
        require(not self._started, 'nlp_service_started')
        self._started = True
        self.owner = self.owners.identity(os.getpid())
        require(self.owner is not None)
        trust = hashlib.sha256(canonical(self.keys)).hexdigest()
        state = self.persistence.initialize(trust)
        self.withdrawals = set(state['withdrawals'])
        self.cached_profiles = self.persistence.profiles()
        self._now(state)
        if state['catalog_jws']:
            catalog, digest = verify_catalog(state['catalog_jws'].encode('ascii'), self.keys)
            require(digest == state['catalog_sha256'] and catalog.sequence == state['sequence'])
            self.catalog = catalog
        for operation in self.persistence.operations():
            if operation['kind'] == 'pin' and operation['state'] == 'running':
                require(self.owners.absent(operation['owner']), 'nlp_operation_owner_live')
                operation['state'] = 'complete'
                self.persistence.operation(operation)
            if operation['kind'] == 'remove' and operation['state'] == 'pending':
                require(self.owners.absent(operation['owner']), 'nlp_operation_owner_live')
                if operation.get('storage'):
                    self.artifacts.recover_interrupted(operation['storage'],
                        owner_absent=lambda op=operation: self.owners.absent(op['owner']))
                operation.update(owner=self.owner, storage=None)
                self.persistence.operation(operation)
                profile = next(p for p, closure, _ in self.persistence.profiles() if closure == operation['closure'])
                self._perform_remove(profile, operation)
            if operation['kind'] == 'acquire' and operation['state'] in (
                    'running', 'paused', 'interrupted', 'cancelled', 'cleanup_required'):
                require(self.owners.absent(operation['owner']), 'nlp_operation_owner_live')
                if operation.get('storage'):
                    self.artifacts.recover_interrupted(operation['storage'],
                        owner_absent=lambda op=operation: self.owners.absent(op['owner']))
                    operation['storage'] = None
                if operation['state'] in ('cancelled', 'cleanup_required'):
                    directory = self.spool / operation['id']
                    if directory.exists():
                        self._clean_spool(directory, operation)
                    operation.update(state='cancelled', files={}, verified={})
                else:
                    operation['state'] = 'paused'
                operation['in_flight'] = False
                self.persistence.operation(operation)
        self.reconcile()

    def _now(self, state):
        now = self.clock()
        require(type(now) in (int, float) and 0 <= now <= 2**53 - 1
                and now >= max(state['clock_high_water'], self.clock_floor), 'nlp_clock_rollback')
        self.clock_floor = int(now)
        return int(now)

    def _authority(self, *, fresh=False):
        require(not self.blocked, 'nlp_dependency_unavailable')
        state = self.persistence.authority()
        now = self._now(state)
        if fresh:
            require(self.catalog is not None and self.catalog.issued_at <= now < self.catalog.expires_at,
                    'nlp_metadata_expired')
        return state, now

    def learn(self, raw):
        self._running()
        catalog, digest = verify_catalog(raw, self.keys)
        with self.gate:
            def transform(state):
                now = self._now(state)
                require(catalog.issued_at <= now < catalog.expires_at, 'nlp_metadata_expired')
                require(catalog.sequence >= state['sequence'] and
                    (catalog.sequence != state['sequence'] or digest == state['catalog_sha256']), 'nlp_catalog_replay')
                withdrawn = sorted(set(state['withdrawals']) | set(catalog.withdrawals))
                require(len(withdrawn) <= MAX_WITHDRAWALS, 'nlp_withdrawal_limit')
                return {**state, 'sequence': catalog.sequence, 'catalog_sha256': digest,
                    'catalog_jws': state['catalog_jws'] if digest == state['catalog_sha256'] else raw.decode('ascii'), 'clock_high_water': now,
                    'epoch': state['epoch'] + int(digest != state['catalog_sha256']), 'withdrawals': withdrawn}
            state = self.persistence.update(transform)
            self.withdrawals = set(state['withdrawals'])
            self.catalog = catalog
            # Authority commits first; callbacks immediately observe it even if derived invalidation fails.
            for digest in state['withdrawals']:
                self.available.pop(digest, None)
                self.findings.withdraw_profile(digest)
        return state['sequence']

    def refresh(self):
        require(self.distribution is not None, 'nlp_unavailable')
        raw = self.distribution.read('catalog.jws', CATALOG_ENVELOPE_BYTES,
            check=self._running, deadline=self.monotonic() + ACQUISITION_SECONDS)
        return self.learn(raw)

    def _running(self):
        require(not self.blocked, 'nlp_dependency_unavailable')
        require(not self.stopping.is_set(), 'nlp_cancelled')

    def _entry(self, profile_id, revision):
        state, _ = self._authority(fresh=True)
        matches = [e for e in self.catalog.profiles if (e.profile_id, e.revision) == (profile_id, revision)]
        require(len(matches) == 1, 'nlp_profile_unknown')
        require(matches[0].profile_sha256 not in state['withdrawals'], 'nlp_profile_withdrawn')
        return matches[0]

    def _admit(self, profile, *, fresh=False):
        self._running()
        state, _ = self._authority(fresh=fresh)
        digest = profile.identity_digest()
        require(digest not in state['withdrawals'], 'nlp_profile_withdrawn')
        require(self.approvals is not None and self.engine is not None
                and self.approvals(profile) is True, 'nlp_release_unapproved')
        validate_resource_policy(profile, self.policy)
        require(self.engine.current(profile.components[0].engine), 'nlp_engine_unavailable')
        if fresh:
            self.persistence.update(lambda current: {**current, 'clock_high_water': self._now(current)})
        return state['epoch']

    def ready(self, profile):
        with self.gate:
            try:
                self._admit(profile)
                return profile.identity_digest() in self.available
            except (ValueError, OSError, TypeError):
                return False

    @contextmanager
    def publication(self, profile):
        # Short authority gate spans the actual analytics commit. No model job holds it.
        with self.gate:
            require(self.ready(profile), 'nlp_dependency_unavailable')
            yield

    @contextmanager
    def retain(self, profile):
        with self._retention(profile, ready=True):
            def execute(request, cancelled):
                deadline = self.monotonic() + request.deadline_ms / 1000

                def check():
                    self._running()
                    require(self.monotonic() < deadline, 'nlp_deadline')
                    require(not cancelled(), 'nlp_cancelled')
                    with self.gate:
                        self._admit(profile)

                from app.nlp import v2
                v2.validate_request(profile, request.model_dump_json().encode())
                available = self.artifacts.reopen(profile, cancellation_check=check)
                with self.engine.open(profile, available, check, deadline=deadline) as session:
                    hello = adapter.HandshakeRequest(request_id=uuid4().hex, expected_engine=profile.components[0].engine)
                    adapter.validate_handshake(profile, hello, session.handshake(hello, deadline=deadline))
                    component = profile.component(request.component_id)
                    self._prepare_component(profile, component, session, check, deadline)
                    try:
                        check()
                        remaining = max(1, int((deadline - self.monotonic()) * 1000))
                        dispatched = request.model_copy(update={'deadline_ms': min(request.deadline_ms, remaining)})
                        return session.analyze(dispatched, cancelled, deadline=deadline)
                    finally:
                        self._unload(profile, component, session)
            yield execute

    @contextmanager
    def _retention(self, profile, *, ready):
        with self.gate:
            self._admit(profile)
            require(not ready or self.ready(profile), 'nlp_dependency_unavailable')
            require(any(p.identity_digest() == profile.identity_digest() and state == 'available'
                        for p, _, state in self.persistence.profiles()), 'nlp_profile_unavailable')
            operation = {'id': uuid4().hex, 'kind': 'pin', 'owner': self.owner,
                         'profile': profile.identity_digest(), 'state': 'running'}
            self.persistence.operation(operation)
            self.pins[operation['id']] = (operation, profile, True)
        try:
            yield
        finally:
            with self.gate:
                self.pins[operation['id']] = (operation, profile, False)
                self._release_pins()
                self.active.notify_all()
            if not self.stopping.is_set():
                self.collect()

    def _release_pins(self):
        for key, (operation, profile, active) in tuple(self.pins.items()):
            if not active and self.engine.drained(profile):
                operation['state'] = 'complete'
                self.persistence.operation(operation)
                self.pins.pop(key)

    def status(self, account=None):
        # A cached observation; read endpoints never refresh or hash model data.
        with self.gate:
            if self.blocked:
                return {'configured': self.approvals is not None and self.engine is not None,
                    'available_profiles': [], 'failure': 'nlp_dependency_unavailable',
                    'profiles': [], 'selected': None, 'acquisitions': [],
                    'resume_granularity': 'verified_files_current_file_restarts'}
            known = {p.identity_digest(): (p, state) for p, _, state in self.cached_profiles}
            entries = list(self.catalog.profiles) if self.catalog is not None else []
            summaries = []
            # At most 32 stored and 32 catalog entries; keep their full union.
            for digest in dict.fromkeys([e.profile_sha256 for e in entries] + list(known)):
                entry = next((e for e in entries if e.profile_sha256 == digest), None)
                profile, state = known.get(digest, (None, None))
                summaries.append({
                    'profile_id': entry.profile_id if entry else profile.profile_id,
                    'revision': entry.revision if entry else profile.revision,
                    'profile_sha256': digest, 'catalogued': entry is not None,
                    'installed': state == 'available', 'prepared': digest in self.available and not self.blocked,
                    'withdrawn': digest in self.withdrawals,
                    'storage_state': state or 'absent',
                    'languages': sorted({c.language for c in profile.components}) if profile else None,
                    'tasks': sorted({b.task for b in profile.bindings}) if profile else None,
                    'resources': profile.resources.model_dump(mode='json') if profile else None})
            selected = None
            if account is not None:
                try:
                    row = self.findings.state(account)
                except ValueError:
                    pass
                else:
                    p = read_profile(row['profile_json'])
                    selected = {'profile_id': p.profile_id, 'revision': p.revision,
                        'profile_sha256': p.identity_digest(), 'mode': row['mode'],
                        'selection_epoch': row['selection_epoch'], 'admission_epoch': row['admission_epoch']}
            return {'configured': self.approvals is not None and self.engine is not None,
                    'available_profiles': sorted(self.available), 'failure': self.failure,
                    'profiles': summaries, 'selected': selected,
                    'acquisitions': [self._observation(op) for op in self.persistence.operations()
                                     if op['kind'] == 'acquire'],
                    'resume_granularity': 'verified_files_current_file_restarts'}

    def _operation(self, operation_id):
        matches = [op for op in self.persistence.operations() if op['id'] == operation_id]
        require(len(matches) == 1, 'nlp_operation_unknown')
        return matches[0]

    def observation(self, operation_id):
        with self.gate:
            return self._observation(self._operation(operation_id))

    @staticmethod
    def _observation(op):
        return {'id': op['id'], 'kind': op['kind'], 'state': op['state'],
                'profile_sha256': op.get('profile'),
                'profile_id': op.get('entry', {}).get('profile_id'),
                'revision': op.get('entry', {}).get('revision'),
                'verified_files': len(op.get('verified', {})),
                'in_flight': op.get('in_flight', False),
                'resume_granularity': 'verified_files_current_file_restarts'}

    def plan(self, profile_id, revision):
        """Explicit metadata preflight; no data download or engine preparation."""
        with self.gate:
            entry = self._entry(profile_id, revision)
            require(self.distribution is not None and self.approvals is not None, 'nlp_unavailable')
        raw = self.distribution.read('profiles/' + entry.profile_sha256, min(MAX_ENVELOPE_BYTES, self.policy.download_bytes),
            check=self._running, deadline=self.monotonic() + ACQUISITION_SECONDS)
        profile = authenticate_profile(raw, trusted_nlp_keys=self.keys, expected_profile_sha256=entry.profile_sha256,
                                       resource_policy=self.policy).profile
        adapter.validate_compatibility(profile, entry.compatibility)
        require(self.approvals(profile) is True, 'nlp_release_unapproved')
        from app.nlp.profiles import artifact_closure_digest
        members = tuple(a for a in profile.artifacts if a.role in DATA_ROLES)
        closure = artifact_closure_digest(members)
        total = sum(a.size_bytes for a in members)
        receipt = len(canonical([a.model_dump(mode='json') for a in sorted(members,key=lambda a:a.artifact_id)]))
        try:
            self.artifacts.reopen(profile, cancellation_check=self._running)
            incremental = 0
        except FileNotFoundError:
            incremental = total
        with self.gate:
            require(self._entry(profile_id, revision) == entry, 'nlp_operation_stale')
            self._not_removing(closure)
            self.persistence.install(profile, closure, planned=True)
            self.cached_profiles = self.persistence.profiles()
            op = {'id': uuid4().hex, 'kind': 'plan', 'state': 'complete', 'profile': profile.identity_digest(),
                'entry': entry.model_dump(mode='json'), 'policy_sha256': hashlib.sha256(canonical(self.policy)).hexdigest(),
                'costs': {'declared_full_artifact_download_bytes': profile.resources.download_bytes,
                    'data_download_bytes': total, 'incremental_data_download_bytes': incremental,
                    'profile_metadata_bytes': len(raw), 'installed_bytes': profile.resources.installed_bytes,
                    'rollback_bytes': profile.resources.rollback_bytes,
                    'acquisition_temporary_bytes': 2 * incremental + receipt if incremental else 0,
                    'transition_peak_memory_bytes': profile.resources.transition_peak_memory_bytes,
                    'inference_threads': profile.resources.inference_threads}}
            self.persistence.operation(op)
        return {'operation_id': op['id'], 'profile_sha256': op['profile'], **op['costs']}

    def acquire(self, profile_id, revision, *, plan_id):
        with self.gate:
            self._running()
            entry = self._entry(profile_id, revision)
            quote = self._operation(plan_id)
            require(quote['kind'] == 'plan' and quote['entry'] == entry.model_dump(mode='json')
                    and quote['policy_sha256'] == hashlib.sha256(canonical(self.policy)).hexdigest(), 'nlp_plan_stale')
            require(self.approvals is not None and self.engine is not None and self.distribution is not None,
                    'nlp_unavailable')
            require(self.thread is None or not self.thread.is_alive(), 'nlp_acquisition_busy')
            op = {'id': uuid4().hex, 'kind': 'acquire', 'state': 'running', 'owner': self.owner,
                  'entry': entry.model_dump(mode='json'), 'profile': entry.profile_sha256,
                  'verified': {}, 'files': {}, 'storage': None, 'in_flight': True, 'plan_id': plan_id,
                  'plan_costs': copy.deepcopy(quote['costs'])}
            self.persistence.operation(op)
            self._launch(op)
            return op['id']

    def pause(self, operation_id, *, cancel=False):
        with self.gate:
            self._running()
            op = self._operation(operation_id)
            require(op['kind'] == 'acquire')
            if op['state'] in ('running', 'paused', 'interrupted'):
                op['state'] = 'cancelled' if cancel else 'paused'
                self.persistence.operation(op)
                if cancel and not op.get('in_flight'):
                    directory = self.spool / op['id']
                    if directory.exists():
                        self._clean_spool(directory, op)
                    op.update(files={}, verified={})
                    self.persistence.operation(op)

    def resume(self, operation_id):
        with self.gate:
            self._running()
            require(self.thread is None or not self.thread.is_alive(), 'nlp_acquisition_busy')
            op = self._operation(operation_id)
            require(op['kind'] == 'acquire' and op['state'] in ('paused', 'interrupted'))
            entry = self._entry(op['entry']['profile_id'], op['entry']['revision'])
            require(entry.model_dump(mode='json') == op['entry'], 'nlp_operation_stale')
            if op.get('storage'):
                journal = op['storage']
                require(list(self._directory_identity(self.artifacts.root)) == journal['root_id'], 'nlp_store_changed')
                lock = reject_path_aliases(self.artifacts.root / '.write.lock')
                stage = journal.get('stage')
                require(not lock.exists() and (not stage or not reject_path_aliases(self.artifacts.root / stage['name']).exists()),
                        'nlp_storage_recovery_required')
                op['storage'] = None
            op.update(state='running', owner=self.owner, in_flight=True)
            self.persistence.operation(op)
            self._launch(op)

    def _launch(self, operation):
        self.thread = threading.Thread(target=self._download, args=(operation,), name='nlp-acquisition', daemon=False)
        self.thread.start()

    def _download(self, op):
        deadline = self.monotonic() + ACQUISITION_SECONDS
        digest = op['entry']['profile_sha256']
        directory = self.spool / op['id']

        def check():
            self._running()
            with self.gate:
                require(self._operation(op['id'])['state'] == 'running', 'nlp_cancelled')
                state, _ = self._authority(fresh=True)
                require(digest not in state['withdrawals'], 'nlp_profile_withdrawn')
            require(self.monotonic() < deadline, 'nlp_acquisition_deadline')

        def save():
            with self.gate:
                current = self._operation(op['id'])
                # A pause/cancellation written by another caller is never overwritten.
                self.persistence.operation({**op, 'state': current['state']})

        try:
            check()
            try:
                self.spool.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                created = False
            apply_private_directory_security(self.spool, repair_permissions=created)
            if not directory.exists():
                directory.mkdir(mode=0o700)
                apply_private_directory_security(directory)
                op['directory_identity'] = list(self._directory_identity(directory))
                op['spool_identity'] = list(self._directory_identity(self.spool))
                save()
            self._check_spool(directory, op)
            profile = next(p for p, _, state in self.persistence.profiles()
                           if p.identity_digest() == digest and state in ('planned','available'))
            require((profile.profile_id, profile.revision) == (op['entry']['profile_id'], op['entry']['revision']))
            from app.nlp.release_catalog import ReleaseEntry
            adapter.validate_compatibility(profile, ReleaseEntry.model_validate_json(canonical(op['entry'])).compatibility)
            with self.gate:
                self._admit(profile, fresh=True)
            members = [a for a in profile.artifacts if a.role in DATA_ROLES]
            from app.nlp.profiles import artifact_closure_digest
            with self.gate:
                op['closure'] = artifact_closure_digest(tuple(members))
                self._not_removing(op['closure'])
                save()
            # Full spool + staging is reserved honestly; no speculative cross-ID deduplication.
            spool_bytes = sum(a.size_bytes for a in members)
            receipt_bytes = len(canonical([a.model_dump(mode='json') for a in sorted(members, key=lambda a:a.artifact_id)]))
            try:
                available = self.artifacts.reopen(profile, cancellation_check=check)
            except FileNotFoundError:
                available = None
            if available is None:
                require(2 * spool_bytes + receipt_bytes <= profile.resources.temporary_bytes, 'nlp_temporary_limit')
                require(shutil.disk_usage(self.spool).free >= self.artifacts.limits.minimum_free_bytes + 2 * spool_bytes + receipt_bytes,
                        'nlp_disk_reserve')
                require(op['plan_costs']['incremental_data_download_bytes'] == spool_bytes, 'nlp_plan_stale')
                for asset in members:
                    check()
                    name = hashlib.sha256(asset.artifact_id.encode()).hexdigest() + '.part'
                    path = directory / name
                    if asset.artifact_id in op['verified']:
                        require(self._verified(path, asset, op['files'][name]), 'nlp_spool_changed')
                        continue
                    if name in op['files']:
                        require(list(private_file_identity(path, repair_permissions=False)) == op['files'][name], 'nlp_spool_changed')
                        path.unlink()
                    self._check_spool(directory, op, missing=name)
                    with path.open('xb') as output:
                        apply_private_file_security(path)
                        op['files'][name] = list(private_file_identity(path))
                        save()
                        count, sha = 0, hashlib.sha256()
                        with self.distribution.stream('data/' + asset.sha256, check=check, deadline=deadline) as blocks:
                            for block in blocks:
                                check()
                                count += len(block)
                                require(count <= asset.size_bytes, 'nlp_artifact_size')
                                require(self._spool_used() + len(block) + spool_bytes + receipt_bytes <= self.policy.temporary_bytes,
                                        'nlp_temporary_limit')
                                require(shutil.disk_usage(self.spool).free >= self.artifacts.limits.minimum_free_bytes + len(block), 'nlp_disk_reserve')
                                output.write(block)
                                sha.update(block)
                        require(count == asset.size_bytes and sha.hexdigest() == asset.sha256, 'nlp_artifact_hash')
                        output.flush()
                        os.fsync(output.fileno())
                    op['verified'][asset.artifact_id] = name
                    save()

                def ownership(event):
                    if event['event'] == 'lock':
                        op['storage'] = {k: event[k] for k in ('root_id', 'lock_id')}
                    elif event['event'] == 'stage':
                        op['storage']['stage'] = {'name': event['name'], 'identity': event['identity'], 'owned': {}}
                    elif event['event'] == 'member':
                        op['storage']['stage']['owned'][event['name']] = event['identity']
                    save()

                with ExitStack() as handles:
                    streams = [(a.artifact_id, handles.enter_context((directory / op['verified'][a.artifact_id]).open('rb'))) for a in members]
                    available = self.artifacts.stage(profile, streams, cancellation_check=check, ownership=ownership)
            check()
            self._prepare(profile, available, check)
            with self.gate:
                self._admit(profile, fresh=True)
                check()
                self.persistence.install(profile, available.data_closure_sha256)
                self.cached_profiles = self.persistence.profiles()
                self.available[digest] = available
                op['state'], op['storage'] = 'complete', None
                self.persistence.operation(op)
            self._clean_spool(directory, op)
        except Exception:
            with self.gate:
                current = self._operation(op['id'])
                op['state'] = current['state'] if current['state'] in ('paused', 'cancelled') else 'interrupted'
                if op['state'] == 'cancelled' and directory.exists():
                    try:
                        self._clean_spool(directory, op)
                        op.update(files={}, verified={})
                    except (ValueError, OSError):
                        op['state'] = 'cleanup_required'
                self.persistence.operation(op)
                self.failure = 'nlp_acquisition_interrupted'
        finally:
            with self.gate:
                current = self._operation(op['id'])
                current['in_flight'] = False
                self.persistence.operation(current)

    @staticmethod
    def _verified(path, asset, identity):
        require(list(private_file_identity(path, repair_permissions=False)) == identity, 'nlp_spool_changed')
        with path.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        return path.stat().st_size == asset.size_bytes and sha == asset.sha256

    def _clean_spool(self, directory, op):
        # Cleanup may resume after some owned members were already removed.
        # Ordinary download/resume still requires the complete exact membership.
        names = self._check_spool(directory, op, cleanup=True)
        for name in names:
            (directory / name).unlink()
        directory.rmdir()
        sync_directory(self.spool)

    @staticmethod
    def _directory_identity(path):
        reject_path_aliases(path)
        value = path.stat()
        require(path.is_dir(), 'nlp_spool_directory')
        return value.st_dev, value.st_ino

    def _check_spool(self, directory, op, *, missing=None, cleanup=False):
        require(re.fullmatch(r'[a-f0-9]{32}', op['id']) and directory == self.spool / op['id'], 'nlp_spool_directory')
        require(list(self._directory_identity(self.spool)) == op['spool_identity']
                and list(self._directory_identity(directory)) == op['directory_identity'], 'nlp_spool_changed')
        names = set()
        for path in directory.iterdir():
            require(len(names) < 256 and re.fullmatch(r'[a-f0-9]{64}\.part', path.name), 'nlp_spool_foreign')
            names.add(path.name)
            require(path.name in op['files'] and path.stat().st_nlink == 1
                    and list(private_file_identity(path, repair_permissions=False)) == op['files'][path.name], 'nlp_spool_changed')
        expected = set(op['files']) - ({missing} if missing else set())
        require(names <= expected if cleanup else names == expected, 'nlp_spool_foreign')
        return names

    def _spool_used(self):
        operations = {op['id']: op for op in self.persistence.operations() if op['kind'] == 'acquire'}
        total, count = 0, 0
        for directory in self.spool.iterdir():
            count += 1
            require(count <= 32 and directory.name in operations, 'nlp_spool_foreign')
            self._check_spool(directory, operations[directory.name])
            total += sum(p.stat().st_size for p in directory.iterdir())
            require(total <= self.policy.temporary_bytes, 'nlp_temporary_limit')
        return total

    def _prepare(self, profile, available, check):
        deadline = self.monotonic() + 120
        request = adapter.HandshakeRequest(request_id=uuid4().hex, expected_engine=profile.components[0].engine)
        with self.engine.open(profile, available, check, deadline=deadline) as session:
            adapter.validate_handshake(profile, request, session.handshake(request, deadline=deadline))
            for component in profile.components:
                self._prepare_component(profile, component, session, check, deadline)
                self._unload(profile, component, session)
            check()

    def _prepare_component(self, profile, component, session, check, deadline):
        check()
        require(self.monotonic() < deadline, 'nlp_deadline')
        artifacts = tuple(a for a in profile.artifacts if a.artifact_id in component.artifact_ids or a.role in ('engine', 'runtime'))
        request = adapter.PrepareRequest(request_id=uuid4().hex, profile_sha256=profile.identity_digest(),
            component_id=component.component_id, component_sha256=component.identity_digest(), artifacts=artifacts,
            max_memory_bytes=profile.resources.transition_peak_memory_bytes, inference_threads=profile.resources.inference_threads)
        adapter.validate_prepare_request(profile, request.model_dump_json().encode(), resource_policy=self.policy)
        def cancelled():
            check()
            return False

        result = adapter.validate_preparation(request, session.prepare(request, deadline=deadline, cancelled=cancelled))
        require(result.outcome == 'ready', 'nlp_prepare_failed')

    def _unload(self, profile, component, session):
        request = adapter.UnloadRequest(request_id=uuid4().hex,
            profile_sha256=profile.identity_digest(), component_sha256=component.identity_digest())
        result = adapter.validate_lifecycle(request, session.unload(request, deadline=self.monotonic() + 5))
        require(result.outcome == 'completed' and result.health == 'unloaded', 'nlp_unload_failed')

    def reconcile(self):
        with self.gate:
            state, _ = self._authority()
            for digest in state['withdrawals']:
                self.findings.withdraw_profile(digest)
            # Restart never reuses a preparation observation or performs warmup automatically.
            for op in self.persistence.operations():
                if op['kind'] == 'select' and op['state'] == 'pending':
                    self._finish_selection(op)

    def prepare_installed(self, digest, *, cancellation_check=None):
        observed_cancel = False
        callback_error = None
        def check():
            nonlocal observed_cancel, callback_error
            self._running()
            if callback_error is not None:
                raise callback_error
            if observed_cancel:
                raise ValueError('nlp_cancelled')
            try:
                cancelled = cancellation_check is not None and cancellation_check()
            except Exception as error:
                callback_error = error
                raise
            if cancelled:
                observed_cancel = True
                raise ValueError('nlp_cancelled')
        check()
        profile = self._profile(digest)
        with self._retention(profile, ready=False):
            check()
            available = self.artifacts.reopen(profile, cancellation_check=check)
            check()
            try:
                self._prepare(profile, available, check)
            finally:
                if callback_error is not None:
                    raise callback_error
            with self.gate:
                check()
                self._admit(profile)
                self.available[digest] = available

    def _profile(self, digest):
        rows = [p for p, _, state in self.persistence.profiles() if p.identity_digest() == digest and state == 'available']
        require(len(rows) == 1, 'nlp_profile_unavailable')
        return rows[0]

    def _source_token(self, account):
        require(self.source.account_exists(account), 'nlp_account_absent')
        return self.source.erasure_source_identity(account)

    def select(self, account, profile_id, revision, *, rollback=False):
        with self.gate:
            if rollback:
                previous = self.findings.state(account).get('previous_profile_json')
                require(previous is not None, 'nlp_rollback_unavailable')
                profile = read_profile(previous)
            else:
                profile = self._profile(self._entry(profile_id, revision).profile_sha256)
            self._admit(profile, fresh=True)
            require(self.ready(profile), 'nlp_dependency_unavailable')
            op = {'id': uuid4().hex, 'kind': 'select', 'state': 'pending', 'account': account,
                  'source_token': self._source_token(account), 'expected': self.findings.selection_token(account),
                  'profile': profile.identity_digest()}
            self.persistence.operation(op)
            self._finish_selection(op)
            return op['id']

    def _finish_selection(self, op):
        try:
            require(op['source_token'] == self._source_token(op['account']), 'nlp_selection_stale')
            profile = self._profile(op['profile'])
            self._admit(profile, fresh=True)
            require(self.ready(profile), 'nlp_dependency_unavailable')
            with self.source.dependency_selection_guard(op['account'], op['source_token']):
                if not self.findings.selection_completed(op['account'], op['id'], op['profile']):
                    self.findings.select(op['account'], profile, expected=op['expected'], preserve_previous=True, intent=op['id'])
            op['state'] = 'complete'
        except ValueError:
            # Never replay a stale delayed callback into a recreated account.
            op['state'] = 'refused'
        self.persistence.operation(op)

    def remove(self, account, profile_id=None, revision=None):
        with self.gate:
            self._running()
            if profile_id is not None:
                matches = [p for p, _, _ in self.persistence.profiles() if (p.profile_id, p.revision) == (profile_id, revision)]
                require(len(matches) == 1, 'nlp_profile_unknown')
                targets = [matches[0].identity_digest()]
            else:
                state = self.findings.state(account)
                targets = [read_profile(state[k]).identity_digest()
                           for k in ('profile_json', 'previous_profile_json') if state[k]]
                self.findings.mode(account, 'removed')
            self.persistence.operation({'id': uuid4().hex, 'kind': 'gc', 'state': 'pending', 'profiles': targets})
        # Cancellation becomes visible before any wait for in-flight retention pins.
        return self.collect()

    def account_mode(self, account, mode):
        """Explicit processing controls, independent of acquisition pause."""
        require(mode in ('paused', 'enabled'))
        with self.gate:
            self._running()
            token = self._source_token(account)
            with self.source.dependency_selection_guard(account, token):
                row = self.findings.state(account)
                require(row['mode'] in ('paused', 'enabled'), 'nlp_account_unavailable')
                if mode == 'enabled':
                    profile = read_profile(row['profile_json'])
                    require(self.ready(profile), 'nlp_dependency_unavailable')
                self.findings.mode(account, mode)

    def _not_removing(self, closure):
        require(not any(op['kind'] == 'remove' and op['state'] == 'pending' and op['closure'] == closure
                        for op in self.persistence.operations()), 'nlp_closure_removing')

    def _removed(self, operation):
        for profile, closure, _ in self.persistence.profiles():
            if closure == operation['closure']:
                self.persistence.removed(profile.identity_digest())
        operation.update(state='complete', storage=None)
        self.persistence.operation(operation)
        self.cached_profiles = self.persistence.profiles()
        states = {p.identity_digest(): state for p, _, state in self.persistence.profiles()}
        for request in self.persistence.operations():
            if request['kind'] == 'gc' and request['state'] == 'pending' and all(states.get(d) == 'removed' for d in request['profiles']):
                request['state'] = 'complete'
                self.persistence.operation(request)

    def collect(self):
        with self.gate:
            referenced = set()
            for row in self.findings.selected_profiles():
                if row['mode'] != 'removed':
                    for key in ('profile_json', 'previous_profile_json'):
                        if row[key]:
                            referenced.add(read_profile(row[key]).identity_digest())
            for op in self.persistence.operations():
                if op['kind'] == 'pin' and op['state'] == 'running' and not self.owners.absent(op['owner']):
                    referenced.add(op['profile'])
            rows = self.persistence.profiles()
            closures = {closure for profile, closure, _ in rows if profile.identity_digest() in referenced}
            requested = {digest for op in self.persistence.operations() if op['kind'] == 'gc' and op['state'] == 'pending'
                         for digest in op['profiles']}
            for op in self.persistence.operations():
                if op['kind'] == 'acquire' and op.get('in_flight') and not self.owners.absent(op['owner']):
                    if op.get('closure'):
                        closures.add(op['closure'])
            candidates = []
            for profile, closure, state in rows:
                if state in ('planned', 'available') and profile.identity_digest() in requested and closure not in closures:
                    for key, value in list(self.available.items()):
                        if value.data_closure_sha256 == closure:
                            self.available.pop(key)
                    if closure not in {c for _, c in candidates}:
                        candidates.append((profile, closure))
                        operation = {'id': uuid4().hex, 'kind': 'remove', 'state': 'pending',
                                     'owner': self.owner, 'closure': closure, 'storage': None}
                        self.persistence.operation(operation)
                        self.persistence.removing(closure)
        for profile, closure in candidates:
            op = next(op for op in self.persistence.operations() if op['kind'] == 'remove'
                      and op['state'] == 'pending' and op['closure'] == closure)
            self._perform_remove(profile, op)
        return {'retained_closures': len(closures), 'removed_closures': len(candidates)}

    def _perform_remove(self, profile, op):
        def ownership(event):
            if event['event'] == 'lock':
                op['storage'] = {k: event[k] for k in ('root_id', 'lock_id')}
            elif event['event'] == 'remove':
                op['storage']['stage'] = {'name': event['name'], 'identity': event['identity'],
                                         'owned': event['owned'], 'removing': True}
            self.persistence.operation(op)

        target = reject_path_aliases(self.artifacts.root / op['closure'])
        if target.exists():
            self.artifacts.remove(profile, cancellation_check=self._running, ownership=ownership)
        with self.gate:
            self._removed(op)

    def close(self, timeout=5):
        deadline = self.monotonic() + max(0, timeout)
        self.stopping.set()
        engine_drained = self.engine is None or self.engine.close(max(0, deadline - self.monotonic()))
        if self.thread is not None:
            self.thread.join(timeout=max(0, deadline - self.monotonic()))
        with self.active:
            while any(active for _, _, active in self.pins.values()) and self.monotonic() < deadline:
                self.active.wait(min(.02, max(0, deadline - self.monotonic())))
            self._release_pins()
            return engine_drained and not self.pins and (self.thread is None or not self.thread.is_alive())


# No origin, keys, approval or engine provider is supplied by default construction.
_service = None


def dependency_service():
    return _service


def configure_dependency_service(service):
    global _service
    require(_service is None or service is None, 'nlp_service_already_configured')
    _service = service
