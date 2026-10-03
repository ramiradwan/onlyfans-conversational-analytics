"""Read-only persistence/host attribution around the frozen cold-prefix diagnostic.

The base runner and production subject remain unchanged. This wrapper adds timing
hooks and samples Windows PDH counters. It never checkpoints or changes settings.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

FROZEN_RUNNER = 'db1398c31416eb6d11ae5901b29f9faab9a61f1a78f35c4846dad181f0aaac9e'
MAX_SAMPLES = 5000
MAX_PRIMITIVES = 100000


def counter_paths():
    result = {}
    for prefix, obj in [('c_volume', 'LogicalDisk(C:)'), ('c_physical', 'PhysicalDisk(0 C:)'),
                        ('all_disks', 'PhysicalDisk(_Total)')]:
        for key, name in [('read_seconds', 'Avg. Disk sec/Read'), ('write_seconds', 'Avg. Disk sec/Write'),
                          ('read_bytes_sec', 'Disk Read Bytes/sec'), ('write_bytes_sec', 'Disk Write Bytes/sec'),
                          ('reads_sec', 'Disk Reads/sec'), ('writes_sec', 'Disk Writes/sec'),
                          ('queue', 'Current Disk Queue Length'), ('avg_queue', 'Avg. Disk Queue Length'),
                          ('idle_percent', '% Idle Time')]:
            result[prefix+'.'+key] = '\\'+obj+'\\'+name
    result.update(cpu_percent=r'\Processor(_Total)\% Processor Time',
                  processor_queue=r'\System\Processor Queue Length',
                  available_bytes=r'\Memory\Available Bytes',
                  page_reads_sec=r'\Memory\Page Reads/sec',
                  pages_input_sec=r'\Memory\Pages Input/sec',
                  pages_output_sec=r'\Memory\Pages Output/sec')
    return result


class Pdh:
    """Use native English counter names and reject invalid data, never zero-fill it."""
    class ValueUnion(ctypes.Union):
        _fields_ = [('number', ctypes.c_double), ('large', ctypes.c_longlong), ('pointer', ctypes.c_void_p)]
    class Value(ctypes.Structure):
        pass
    Value._fields_ = [('status', w.DWORD), ('value', ValueUnion)]
    class Item(ctypes.Structure):
        pass
    Item._fields_ = [('name', w.LPWSTR), ('formatted', Value)]

    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('host_io_diagnostic_requires_windows')
        self.dll = ctypes.WinDLL('pdh')
        self.dll.PdhOpenQueryW.argtypes = [w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)]
        self.dll.PdhAddEnglishCounterW.argtypes = [w.HANDLE, w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)]
        self.dll.PdhCollectQueryData.argtypes = [w.HANDLE]
        self.dll.PdhGetFormattedCounterValue.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, ctypes.POINTER(self.Value)]
        self.dll.PdhCloseQuery.argtypes = [w.HANDLE]
        self.dll.PdhGetFormattedCounterArrayW.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        for name in ['PdhOpenQueryW', 'PdhAddEnglishCounterW', 'PdhCollectQueryData',
                     'PdhGetFormattedCounterValue', 'PdhGetFormattedCounterArrayW', 'PdhCloseQuery']:
            getattr(self.dll, name).restype = w.DWORD
        self.query = w.HANDLE()
        self.paths, self.handles = counter_paths(), {}
        self.array_paths = {k:'\\Process(*)\\'+v for k,v in [('pid','ID Process'), ('read_bytes_per_sec','IO Read Bytes/sec'), ('write_bytes_per_sec','IO Write Bytes/sec'), ('other_bytes_per_sec','IO Other Bytes/sec'), ('cpu_percent','% Processor Time')]}
        self.arrays, self.processes, self.process_errors = {}, [], {}
        rc = self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query))
        if rc: raise OSError(f'PdhOpenQueryW:{rc:08x}')
        try:
            for key, path in self.paths.items():
                handle = w.HANDLE()
                rc = self.dll.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(handle))
                if rc: raise OSError(f'PdhAddEnglishCounterW:{key}:{rc:08x}')
                self.handles[key] = handle
            for key,path in self.array_paths.items():
                handle = w.HANDLE()
                rc = self.dll.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(handle))
                if rc: raise OSError(f'PdhAddEnglishCounterW:{key}:{rc:08x}')
                self.arrays[key] = handle
            rc = self.dll.PdhCollectQueryData(self.query)
            if rc: raise OSError(f'PdhCollectQueryData:{rc:08x}')
        except BaseException:
            self.close()
            raise

    def collect(self):
        rc = self.dll.PdhCollectQueryData(self.query)
        if rc: raise OSError(f'PdhCollectQueryData:{rc:08x}')
        values, errors = {}, {}
        for key, handle in self.handles.items():
            value = self.Value()
            # DOUBLE | NOSCALE | NOCAP100: seconds remain seconds, not display-scaled units.
            rc = self.dll.PdhGetFormattedCounterValue(handle, 0x200 | 0x1000 | 0x8000, None, ctypes.byref(value))
            if rc or value.status not in (0, 1) or not math.isfinite(value.value.number):
                errors[key] = dict(return_code=int(rc), counter_status=int(value.status))
            else:
                values[key] = value.value.number
        arrays, self.process_errors = {}, {}
        for key,handle in self.arrays.items():
            size,count = w.DWORD(),w.DWORD()
            rc=self.dll.PdhGetFormattedCounterArrayW(handle,0x200|0x1000|0x8000,ctypes.byref(size),ctypes.byref(count),None)
            if rc not in (0,0x800007D2):
                self.process_errors[key]={'return_code':int(rc)}
                continue
            buffer=ctypes.create_string_buffer(max(size.value,1))
            rc=self.dll.PdhGetFormattedCounterArrayW(handle,0x200|0x1000|0x8000,ctypes.byref(size),ctypes.byref(count),buffer)
            if rc:
                self.process_errors[key]={'return_code':int(rc)}
                continue
            items=ctypes.cast(buffer,ctypes.POINTER(self.Item))
            arrays[key]={items[i].name:items[i].formatted.value.number for i in range(count.value)
                         if items[i].formatted.status in (0,1) and math.isfinite(items[i].formatted.value.number)}
        self.processes=[]
        for instance,pid in arrays.get('pid',{}).items():
            if instance in ('_Total','Idle'):continue
            row=dict(pid=int(pid),name=instance)
            for key,items in arrays.items():
                if key!='pid':row[key]=items.get(instance)
            if any((row.get(k) or 0)>0 for k in ['read_bytes_per_sec','write_bytes_per_sec','other_bytes_per_sec','cpu_percent']):
                self.processes.append(row)
        self.processes.sort(key=lambda r:(r.get('read_bytes_per_sec') or 0)+(r.get('write_bytes_per_sec') or 0),reverse=True)
        return values, errors

    def close(self):
        if self.query:
            self.dll.PdhCloseQuery(self.query)
            self.query = w.HANDLE()


class HostSampler:
    def __init__(self, trace):
        import psutil
        self.psutil, self.trace = psutil, trace
        self.pdh = Pdh()
        self.samples, self.errors, self.previous = [], [], {}
        self.stop_event = threading.Event()
        self.thread = None
        self.ready = threading.Event()
        self.cpu_seconds = 0.0
        self.joined = False

    def start(self):
        self.thread = threading.Thread(target=self._run, name='a07-host-io-sampler', daemon=True)
        self.thread.start()
        if not self.ready.wait(15):
            raise RuntimeError('host_counter_preflight_timeout')
        if self.errors or not self.samples or self.samples[0]['counter_errors']:
            raise RuntimeError('host_counter_preflight_failed:'+str(self.errors or self.samples[:1]))

    def _run(self):
        cpu = time.thread_time()
        try:
            while not self.stop_event.wait(0.5):
                started = time.monotonic()
                phase = self.trace.phase
                values, errors = self.pdh.collect()
                sampled = time.monotonic()
                processes = self.pdh.processes
                me = self.psutil.Process()
                mem = me.memory_info()
                active = self.trace.snapshot_active()
                stacks = {}
                if phase.startswith('ordinary/'):
                    frames = sys._current_frames()
                    for tid,frame in frames.items():
                        if tid == threading.get_ident(): continue
                        stack = []
                        while frame is not None and len(stack) < 12:
                            stack.append(f'{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}')
                            frame = frame.f_back
                        if str(tid) in active or any('analytics' in entry or 'database.py' in entry or 'pipeline.py' in entry for entry in stack):
                            stacks[str(tid)] = stack
                self.samples.append(dict(start=started, at=sampled, end=time.monotonic(), phase=phase,
                    counters=values, counter_errors=errors, process_rates=processes, process_counter_errors=self.pdh.process_errors,
                    benchmark_io=me.io_counters()._asdict(), benchmark_rss=mem.rss,
                    benchmark_page_faults=getattr(mem, 'num_page_faults', None), active_calls=active, stacks=stacks))
                self.ready.set()
                if len(self.samples) >= MAX_SAMPLES:
                    raise RuntimeError('host_sample_limit')
        except BaseException as exc:
            self.errors.append(dict(type=type(exc).__name__, message=str(exc)))
            self.ready.set()
        finally:
            self.cpu_seconds = time.thread_time()-cpu
            self.pdh.close()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            self.joined = not self.thread.is_alive()
        else:
            self.pdh.close()
            self.joined = True

    def result(self):
        return dict(interval_target_seconds=0.5, process_interval_target_seconds=0.5,
                    counter_paths=counter_paths(), process_counter_paths=self.pdh.array_paths, sampler_cpu_seconds=self.cpu_seconds,
                    joined=self.joined, errors=self.errors, samples=self.samples,
                    limitation='Per-process I/O includes network/device I/O; protected process counters can be inaccessible.')


def instrument(base):
    original = base.Trace
    class IOTrace(original):
        def __init__(self):
            super().__init__()
            self.active = {}
            self.active_lock = threading.Lock()
            self.primitives = []
            self.dropped = 0
            self.host = None
            self.created_attributes = []
            self.local = threading.local()
            instrument.last_trace = self

        def patch(self, owner, name, replacement):
            if name not in vars(owner): self.created_attributes.append((owner, name))
            return super().patch(owner, name, replacement)

        def snapshot_active(self):
            with self.active_lock:
                return {str(k):[dict(x) for x in stack] for k,stack in self.active.items() if stack}

        def call(self, kind, method, args, kwargs, store=None, detail=None, threshold=0):
            started, cpu, phase, tid = time.monotonic(), time.thread_time(), self.phase, threading.get_ident()
            item = dict(kind=kind, start=started, store=store, detail=detail)
            with self.active_lock: self.active.setdefault(tid, []).append(item)
            error = None
            try:
                return method(*args, **kwargs)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                ended, elapsed_cpu = time.monotonic(), time.thread_time()-cpu
                with self.active_lock: self.active[tid].pop()
                if ended-started >= threshold or error:
                    if len(self.primitives) < MAX_PRIMITIVES:
                        self.primitives.append(dict(item, end=ended, phase=phase, thread=tid,
                                                    thread_cpu_seconds=elapsed_cpu, error=error))
                    else: self.dropped += 1

        def install(self, q):
            super().install(q)
            from app.persistence.database import LocalSQLite, _TrackedConnection
            from app.persistence import private_files
            self.host = HostSampler(self)
            self.host.start()
            for owner, name, kind in [(_TrackedConnection,'commit','sqlite_commit'),
                (_TrackedConnection,'rollback','sqlite_rollback'), (_TrackedConnection,'_close_native','sqlite_native_close'),
                (LocalSQLite,'connect','connection_open'), (LocalSQLite,'_configure_cipher','cipher_configuration'),
                (LocalSQLite,'_restrict_permissions','permission_checks'),
                (private_files,'_set_windows_owner_only_acl','set_file_acl'),
                (private_files,'_windows_acl_is_owner_only','verify_file_acl')]:
                fn = getattr(owner, name)
                def make(fn, kind):
                    def wrapper(obj, *args, **kwargs):
                        path = getattr(obj, '_tracked_path', None) or getattr(obj, 'path', None)
                        if kind in ('set_file_acl','verify_file_acl'): path = obj
                        store = Path(path).name if path else None
                        return self.call(kind, fn, (obj, *args), kwargs, store)
                    return wrapper
                self.patch(owner, name, make(fn, kind))
            original_write = q.write_once
            def write(path, value):
                before = getattr(self.local, 'file', None)
                self.local.file = Path(path).name
                try: return original_write(path, value)
                finally: self.local.file = before
            self.patch(q, 'write_once', write)
            sync = os.fsync
            def fsync(fd):
                return self.call('os_fsync', sync, (fd,), {}, getattr(self.local,'file',None), detail='Python fsync only; SQLCipher native flushes are inside sqlite_commit/execute/close')
            self.patch(os, 'fsync', fsync)
            execute = _TrackedConnection.execute
            def sql(connection, statement, *args, **kwargs):
                # Avoid per-row profiling during cold construction. Never retain SQL text or values.
                if not self.phase.startswith('ordinary/'):
                    return execute(connection, statement, *args, **kwargs)
                verb = statement.lstrip().split(None,1)[0].upper() if statement.strip() else 'EMPTY'
                detail = verb+':'+hashlib.sha256(statement.encode()).hexdigest()[:16]
                path = getattr(connection, '_tracked_path', None)
                return self.call('sqlite_execute', execute, (connection, statement, *args), kwargs,
                                 Path(path).name if path else None, detail, threshold=0.001)
            self.patch(_TrackedConnection, 'execute', sql)

        def restore(self):
            if self.host: self.host.stop()
            super().restore()
            for owner,name in reversed(self.created_attributes):
                if name in vars(owner): delattr(owner, name)
    base.Trace = IOTrace
    return IOTrace


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--frozen-runner', type=Path, required=True)
    own, remaining = p.parse_known_args()
    runner = own.frozen_runner.resolve()
    digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    if digest != FROZEN_RUNNER: raise ValueError('frozen_base_runner_changed')
    output = Path(remaining[remaining.index('--output')+1])
    spec = importlib.util.spec_from_file_location('frozen_first_update', runner)
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    instrument(base)
    sys.argv = [str(runner), *remaining]
    code = base.main()
    trace = getattr(instrument, 'last_trace', None)
    if trace is None: raise RuntimeError('instrumentation_not_started')
    receipt = output/'result.json'
    payload = dict(schema='a07-cold-first-update-io.v1', base_runner_sha256=digest,
        wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(), base_exit_code=code,
        complete=code==0 and trace.host is not None and trace.host.joined and not trace.host.errors and not trace.dropped and not any(s['counter_errors'] or s['process_counter_errors'] for s in trace.host.samples),
        primitives=trace.primitives, dropped_primitives=trace.dropped,
        host=trace.host.result() if trace.host else None,
        limitations=['Instrumented diagnostic, not qualification; host counters are sampled not ETW.',
                     'Nested durations overlap and must not be summed.',
                     'SQLCipher internal FlushFileBuffers is not directly hooked; native work is bounded by the recorded calls.',
                     'SQL execute timing is update-only and retained above 1 ms; cursor fetch work is not individually timed.',
                     'Windows session is not elevated; no kernel ETW or service changes are made.'])
    from tools import analytics_qualification as q
    q.write_once(output/'io-result.json', payload)
    print(json.dumps(dict(io_complete=payload['complete'], primitive_events=len(trace.primitives),
        host_samples=len(trace.host.samples) if trace.host else 0,
        io_receipt_sha256=hashlib.sha256((output/'io-result.json').read_bytes()).hexdigest())), flush=True)
    return code if payload['complete'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
