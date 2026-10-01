"""Fast source-exact diagnostic for the A07 idle visibility correction loop.

This tool deliberately separates expensive candidate seed preparation from repeated
idle visibility probes. It is diagnostic evidence, not a substitute for the frozen
fresh/restart qualification jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from uuid import uuid4

DATABASES = ("canonical.sqlite3", "projections.sqlite3", "analytics.sqlite3")
SEED_SCHEMA = "a07-targeted-ready-seed.v1"
RECEIPT_SCHEMA = "a07-targeted-idle-visibility.v2"
DEFAULT_JOB_MEMORY = 2560 * 1024 * 1024
DEFAULT_HOST_RESERVE = 3584 * 1024 * 1024
DEFAULT_PREFLIGHT = 5 * 1024 * 1024 * 1024


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def atomic_status(path: Path, stage: str, **values) -> None:
    """Best-effort live state; immutable receipts remain the evidence surface."""
    payload = {"stage": stage, "pid": os.getpid(), "monotonic": time.monotonic(), **values}
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[status-warning] {type(error).__name__}", flush=True)
    details = " ".join(f"{key}={value}" for key, value in values.items())
    print(f"[{stage}] {details}".rstrip(), flush=True)


class PhaseHeartbeat:
    def __init__(self, status: Path, stage: str, *, interval: float = 30.0, **values):
        self.status, self.stage, self.interval, self.values = status, stage, interval, values
        self.started = 0.0
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        self.started = time.monotonic()
        atomic_status(self.status, self.stage, state="started", elapsed_seconds=0.0, **self.values)
        self.thread = threading.Thread(target=self._run, name=f"{self.stage}-heartbeat", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.wait(self.interval):
            atomic_status(
                self.status, self.stage, state="running",
                elapsed_seconds=round(time.monotonic() - self.started, 3), **self.values,
            )

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        atomic_status(
            self.status, self.stage, state="failed" if exc_type else "complete",
            elapsed_seconds=round(time.monotonic() - self.started, 3), **self.values,
        )
        return False


@contextmanager
def verified_seed_storage_start():
    """Skip redundant full-file startup checks for byte-verified diagnostic seed bytes.

    The ready seed already passed the normal migration/startup path and is copied only
    after its exact database hashes are recorded. A candidate using those bytes still
    runs scheduler reconciliation and source-bound logical verification before it can
    become a new ready seed. Production startup and frozen qualification never use
    this context.
    """
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    from app.persistence.migrations import MigrationRunner

    original_reconcile = SQLiteAnalyticsProjectionStore.reconcile_startup
    original_validate = MigrationRunner.__dict__["_validate_database"]

    def already_verified(self):
        return {"verified_seed": 1}

    SQLiteAnalyticsProjectionStore.reconcile_startup = already_verified
    MigrationRunner._validate_database = staticmethod(lambda connection: None)
    try:
        yield
    finally:
        MigrationRunner._validate_database = original_validate
        SQLiteAnalyticsProjectionStore.reconcile_startup = original_reconcile


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def database_hashes(directory: Path) -> dict[str, str]:
    missing = [name for name in DATABASES if not (directory / name).is_file()]
    if missing:
        raise ValueError("missing_benchmark_databases:" + ",".join(missing))
    return {name: file_sha256(directory / name) for name in DATABASES}


def copy_databases(
    source: Path, destination: Path, *, expected: dict[str, str] | None = None
) -> dict[str, str]:
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    expected_hashes = expected if expected is not None else database_hashes(source)
    if set(expected_hashes) != set(DATABASES):
        raise ValueError("benchmark_database_expected_hashes_invalid")
    for name in DATABASES:
        shutil.copy2(source / name, destination / name)
    actual = database_hashes(destination)
    if actual != expected_hashes:
        raise ValueError("benchmark_database_copy_mismatch")
    return actual


def qualification(root: Path):
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.chdir(root)
    import tests.conftest  # noqa: F401 - installs isolated synthetic test keys.
    from tools import analytics_qualification as q
    return q


def install_windows_job_limit(limit: int):
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"
        )]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic), ("io", IO), ("process_limit", ctypes.c_size_t),
            ("job_limit", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.basic.flags = 0x00000200 | 0x00002000
    info.job_limit = limit
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


class MemoryGuard:
    def __init__(self, *, job_limit: int, host_reserve: int, preflight_minimum: int):
        import psutil
        self.psutil = psutil
        self.job_limit = job_limit
        self.host_reserve = host_reserve
        self.preflight_minimum = preflight_minimum
        self.stop = threading.Event()
        available = psutil.virtual_memory().available
        if available < preflight_minimum:
            raise RuntimeError(f"host_memory_preflight_blocked:{available}")
        self.handle = install_windows_job_limit(job_limit)
        self.state = {
            "limit_bytes": job_limit,
            "host_reserve_bytes": host_reserve,
            "preflight_available_bytes": available,
            "peak_tree_rss_bytes": 0,
            "minimum_host_available_bytes": available,
            "abort_reason": None,
        }
        self.thread = threading.Thread(target=self._watch, name="a07-memory-guard", daemon=True)
        self.thread.start()

    def _tree_rss(self) -> int:
        root = self.psutil.Process()
        total = root.memory_info().rss
        for child in root.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                pass
        return total

    def _watch(self) -> None:
        while not self.stop.wait(0.25):
            available = self.psutil.virtual_memory().available
            rss = self._tree_rss()
            self.state["peak_tree_rss_bytes"] = max(self.state["peak_tree_rss_bytes"], rss)
            self.state["minimum_host_available_bytes"] = min(
                self.state["minimum_host_available_bytes"], available
            )
            if available < self.host_reserve:
                self.state["abort_reason"] = "host_memory_reserve"
                os._exit(97)

    def close(self) -> dict:
        self.stop.set()
        self.thread.join(timeout=5)
        self.state["peak_tree_rss_bytes"] = max(
            self.state["peak_tree_rss_bytes"], self._tree_rss()
        )
        self.state["minimum_host_available_bytes"] = min(
            self.state["minimum_host_available_bytes"],
            self.psutil.virtual_memory().available,
        )
        return dict(self.state)


def _other_benchmark_jobs() -> list[dict]:
    import psutil
    found = []
    me = os.getpid()
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == me:
            continue
        try:
            if (process.info["name"] or "").lower() not in {"python.exe", "python", "python3"}:
                continue
            command = " ".join(process.cmdline())
            if any(token in command for token in (
                "targeted_visibility_benchmark", "targeted_idle_dominant",
                "qualify_analytics", "run_visibility",
            )):
                found.append({"pid": process.pid, "command": process.cmdline()})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def validate_source(q, root: Path, expected_sha: str, gnupg_home: Path | None):
    if gnupg_home is not None:
        os.environ["GNUPGHOME"] = str(gnupg_home)
    source = q.source_context(root)
    if source["revision"] != expected_sha:
        raise ValueError(f"source_revision_mismatch:{source['revision']}")
    if source["working_tree"]:
        raise ValueError("source_worktree_not_clean")
    if not source["signature_valid"]:
        raise ValueError("source_signature_invalid")
    return source


def seed_metadata(q, ready_seed: Path) -> dict:
    metadata = q.read_json(ready_seed / "ready-seed.json")
    if metadata.get("schema") != SEED_SCHEMA or metadata.get("complete") is not True:
        raise ValueError("ready_seed_incomplete")
    if metadata.get("database_sha256") != database_hashes(ready_seed):
        raise ValueError("ready_seed_digest_mismatch")
    return metadata


def inherited_seed_verification(
    base: dict | None, reference: dict, manifest_sha: str, runtime_sha: str,
    source_revision: str,
):
    if not base:
        return None
    verification = base.get("verification")
    if (not isinstance(verification, dict)
            or verification.get("independent_rebuild_equal") is not True
            or verification.get("persisted_content_revalidated") is not True
            or base.get("source_revision") != source_revision
            or base.get("manifest_sha256") != manifest_sha
            or base.get("runtime_sha256") != runtime_sha
            or base.get("reference", {}).get("generation_id") != reference.get("generation_id")):
        return None
    return verification


async def prepare_seed(args, q, source, manifest, runtime, status_path: Path) -> dict:
    from tools.analytics_qualification_fixture import Workload
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus

    if args.ready_seed.exists():
        raise FileExistsError(args.ready_seed)
    base_ready = None
    if (args.base_seed / "ready-seed.json").is_file():
        base_ready = seed_metadata(q, args.base_seed)
    workdir = args.ready_seed.parent / (args.ready_seed.name + ".preparing-" + uuid4().hex)
    base_hashes = copy_databases(
        args.base_seed, workdir,
        expected=base_ready["database_sha256"] if base_ready is not None else None,
    )
    if base_ready is not None:
        with verified_seed_storage_start():
            work = Workload(workdir, manifest, 100000, reopen=True, known_kinds=True)
    else:
        work = Workload(workdir, manifest, 100000, reopen=True, known_kinds=True)
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(
        work.f.pipeline, worker_count=1, queue_capacity=64, reconciliation_interval=30
    )
    result = {
        "schema": SEED_SCHEMA,
        "complete": False,
        "source_revision": source["revision"],
        "source_sha256": q.digest(source),
        "manifest_sha256": q.digest(manifest),
        "runtime_sha256": q.digest(runtime),
        "runner_sha256": file_sha256(Path(__file__).resolve()),
        "base_seed": str(args.base_seed.resolve()),
        "base_database_sha256": base_hashes,
    }
    try:
        resources.start()
        began = time.monotonic()
        if base_ready is None:
            with PhaseHeartbeat(status_path, "seed-recovery"):
                await scheduler.start(recover=True)
                state = await scheduler.wait(work.account)
                result["recovery_seconds"] = time.monotonic() - began
                if state.availability != AvailabilityStatus.AVAILABLE:
                    raise ValueError("seed_recovery_not_available")
            result["startup_mode"] = "cold_recovery"
        else:
            with PhaseHeartbeat(status_path, "seed-open"):
                with verified_seed_storage_start():
                    await scheduler.start(recover=False)
            result["recovery_seconds"] = 0.0
            result["startup_mode"] = "verified_base_seed"

        revision = work.counts()["revision"]
        began = time.monotonic()
        with PhaseHeartbeat(status_path, "seed-questions", revision=revision):
            prepared = await asyncio.to_thread(
                work.f.pipeline.prepare_questions, work.account, revision
            )
            result["question_prepare_seconds"] = time.monotonic() - began
            if not prepared:
                raise ValueError("seed_question_preparation_failed")
            if base_ready is not None:
                await scheduler.reconcile_once()
                state = scheduler.state(work.account, canonical_revision=revision)
                if state.availability != AvailabilityStatus.AVAILABLE:
                    raise ValueError("seed_verified_base_not_available")
            result["reference"] = await asyncio.to_thread(work.capture_current_reference)

        inherited = inherited_seed_verification(
            base_ready, result["reference"], result["manifest_sha256"],
            result["runtime_sha256"], result["source_revision"],
        )
        if inherited is not None:
            result["verification"] = inherited
            result["verification_mode"] = "inherited_verified_generation"
            result["verification_seconds"] = 0.0
            result["verification_inherited_from_sha256"] = file_sha256(
                args.base_seed / "ready-seed.json"
            )
            atomic_status(
                status_path, "seed-verification", state="inherited",
                generation=result["reference"]["generation_id"],
            )
        else:
            began = time.monotonic()
            with PhaseHeartbeat(status_path, "seed-verification"):
                result["verification"] = await asyncio.to_thread(work.verify)
                result["verification_seconds"] = time.monotonic() - began
            result["verification_mode"] = "independent_rebuild"
        result["counts"] = work.counts()
        result["backlog"] = scheduler.retained_account_count
        result["complete"] = True
    finally:
        resources.close()
        result["scheduler_closed"] = await scheduler.close(timeout=10)
        result["detached_workers"] = scheduler.detached_worker_count
        work.close()

    if not result["scheduler_closed"] or result["detached_workers"] or result["backlog"]:
        raise ValueError("seed_workers_or_backlog_not_closed")
    result["source_unchanged"] = q.source_context(args.source_root) == source
    if not result["source_unchanged"]:
        raise ValueError("source_changed_during_seed_preparation")

    args.ready_seed.mkdir(parents=True)
    for name in DATABASES:
        shutil.copy2(workdir / name, args.ready_seed / name)
    result["database_sha256"] = database_hashes(args.ready_seed)
    q.write_once(args.ready_seed / "ready-seed.json", result)
    shutil.rmtree(workdir)
    atomic_status(status_path, "seed-ready", seed_path=str(args.ready_seed))
    return result


async def activate_normal_maintenance(scheduler, account, revision, expected_availability):
    """Enter the scheduler's ordinary recovery/maintenance lifecycle after fast open."""
    await scheduler.start(recover=True)
    state = scheduler.state(account, canonical_revision=revision)
    if state.availability != expected_availability:
        raise ValueError("ready_seed_runtime_not_available")
    return state


async def run_probe(args, q, source, manifest, runtime, status_path: Path, guard: MemoryGuard) -> dict:
    from tools.analytics_qualification_fixture import Workload
    from tools.analytics_qualification_workloads import observed_question
    from app.analytics.errors import CanonicalRevisionChanged
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus

    with PhaseHeartbeat(status_path, "seed-validation"):
        seed = seed_metadata(q, args.ready_seed)
    bindings = {
        "source_revision": source["revision"],
        "source_sha256": q.digest(source),
        "manifest_sha256": q.digest(manifest),
        "runtime_sha256": q.digest(runtime),
    }
    for key, expected in bindings.items():
        if seed.get(key) != expected:
            raise ValueError(f"ready_seed_binding_mismatch:{key}")

    workdir = args.workdir or args.result.parent / (args.result.stem + ".work-" + uuid4().hex)
    with PhaseHeartbeat(status_path, "working-copy"):
        copied_hashes = copy_databases(
            args.ready_seed, workdir, expected=seed["database_sha256"]
        )
    if copied_hashes != seed["database_sha256"]:
        raise ValueError("copied_ready_seed_digest_mismatch")

    with PhaseHeartbeat(status_path, "runtime-open"):
        with verified_seed_storage_start():
            work = Workload(workdir, manifest, 100000, reopen=True, known_kinds=True)
    if args.profile_output is not None:
        from tools import analytics_qualification_profile as profile
        profile.FUNCTIONS = profile.FUNCTIONS | frozenset({
            "_retire_active_generation", "capture_transition", "finish_transition",
            "_guards_match", "_references", "_install_enrichment_transition",
            "_generation", "_require_live_owner", "_require_generation_identity",
            "_intent_matches", "_enrichment_transition_candidate",
        })
        profile.install(work, args.profile_output)
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(
        work.f.pipeline, worker_count=1, queue_capacity=64, reconciliation_interval=30
    )
    result = {
        "schema": RECEIPT_SCHEMA,
        "complete": False,
        "source_revision": source["revision"],
        "source": source,
        "manifest_sha256": q.digest(manifest),
        "runtime_sha256": q.digest(runtime),
        "runner_sha256": file_sha256(Path(__file__).resolve()),
        "ready_seed": {
            "path": str(args.ready_seed.resolve()),
            "metadata_sha256": file_sha256(args.ready_seed / "ready-seed.json"),
            "database_sha256": seed["database_sha256"],
            "prepared_by_runner_sha256": seed["runner_sha256"],
        },
    }
    observer_stop = threading.Event()
    samples, sample_errors = [], []

    def sample() -> dict:
        return {
            "at": q.stamp(),
            "host_available_bytes": guard.psutil.virtual_memory().available,
            "tree_rss_bytes": guard._tree_rss(),
            "other_benchmark_jobs": _other_benchmark_jobs(),
        }

    def observe() -> None:
        while not observer_stop.wait(1):
            try:
                samples.append(sample())
            except BaseException as error:
                sample_errors.append(type(error).__name__)

    thread = None
    try:
        resources.start()
        began = time.monotonic()
        with PhaseHeartbeat(status_path, "scheduler-open"):
            with verified_seed_storage_start():
                await scheduler.start(recover=False)
        result["scheduler_start_seconds"] = time.monotonic() - began

        current = work.counts()["revision"]
        began = time.monotonic()
        with PhaseHeartbeat(status_path, "question-preparation"):
            if not await asyncio.to_thread(work.f.pipeline.prepare_questions, work.account, current):
                raise ValueError("ready_seed_question_preparation_failed")
        result["question_prepare_seconds"] = time.monotonic() - began
        result["starting_reference"] = await asyncio.to_thread(work.capture_current_reference)

        # Now enter the scheduler's ordinary recover=True lifecycle. Storage is
        # already open and source/question state is prepared, so this adds the normal
        # reconciliation and identity-preparation maintenance tasks without replaying
        # the expensive cold database startup.
        began = time.monotonic()
        state = await activate_normal_maintenance(
            scheduler, work.account, current, AvailabilityStatus.AVAILABLE
        )
        result["initial_reconciliation_seconds"] = time.monotonic() - began
        result["startup_mode"] = "verified_ready_seed_normal_maintenance"
        atomic_status(
            status_path, "ready", state="available",
            prepare_seconds=round(result["question_prepare_seconds"], 3),
            reconcile_seconds=round(result["initial_reconciliation_seconds"], 3),
        )

        idle_seconds = (
            args.idle_seconds if args.idle_seconds is not None
            else float(manifest["visibility"]["idle_seconds"])
        )
        result["idle_seconds"] = idle_seconds
        atomic_status(status_path, "idle", state="started", seconds=idle_seconds)
        began = time.monotonic()
        await asyncio.sleep(idle_seconds)
        result["observed_idle_seconds"] = time.monotonic() - began
        atomic_status(
            status_path, "idle", state="complete",
            observed=round(result["observed_idle_seconds"], 3),
        )

        first = sample()
        if first["other_benchmark_jobs"]:
            raise ValueError("concurrent_benchmark_process_detected")
        samples.append(first)
        thread = threading.Thread(target=observe, name="a07-targeted-host", daemon=False)
        thread.start()

        before = work.counts()
        work.observe_activation()
        case = args.case
        work.profile_phase = f"idle/{case}" if args.profile_output is not None else None
        chat = 0 if case == "dominant" else 1
        label = f"visibility-targeted-{source['revision'][:8]}-idle-{case}"
        atomic_status(status_path, "probe", state="committing", case=f"idle/{case}")
        committed = await asyncio.to_thread(work.add, chat, label)
        previous = work.last
        stale = False
        if previous is not None:
            try:
                await asyncio.to_thread(
                    work.f.stores.projections.read_generation_artifact, work.account, previous
                )
            except CanonicalRevisionChanged:
                stale = True
        after = work.counts()
        await scheduler.schedule(work.account, after["revision"])
        atomic_status(status_path, "probe", state="scheduled", revision=after["revision"])

        observations = []

        async def visible():
            deadline = time.monotonic() + args.probe_timeout
            while time.monotonic() < deadline:
                item = await asyncio.to_thread(observed_question, work, resources)
                observations.append(item)
                if item["current"]:
                    return item["at"]
                await asyncio.sleep(manifest["measurement"]["visibility_poll_seconds"])
            return None

        visible_task = asyncio.create_task(visible())
        state = await scheduler.wait(work.account)
        drained = time.monotonic()
        seen = await visible_task
        cleanup_at = work.cleaned.get(work.last.generation_id) if work.last else None
        finish = max(value for value in (seen, cleanup_at, drained) if value is not None)
        events = [event for event in work.events if event["at"] >= committed]
        event_map = {event["stage"]: event["at"] for event in events}
        seconds = finish - committed
        result["probe"] = {
            "case": f"idle/{case}",
            "before": before,
            "after": after,
            "durable_canonical_commit": committed,
            "first_valid_visible_result": seen,
            "required_cleanup_complete": cleanup_at,
            "backlog_drained": drained,
            "seconds": seconds,
            "ten_second_gate": seconds <= 10,
            "commit_to_built_seconds": event_map["built"] - committed,
            "built_to_validated_seconds": event_map["validated"] - event_map["built"],
            "validated_to_done_seconds": finish - event_map["validated"],
            "availability": state.availability.value,
            "attempted_revision": state.attempted_revision,
            "valid_current_result": seen is not None,
            "cleanup_complete": cleanup_at is not None,
            "stale_reference_rejected": stale,
            "backlog_before": 0,
            "backlog_after": scheduler.retained_account_count,
            "publication_events": events,
            "question_observations": observations,
        }
        if state.availability != AvailabilityStatus.AVAILABLE:
            raise ValueError("targeted_probe_not_available")
        if state.attempted_revision != after["revision"] or seen is None:
            raise ValueError("targeted_probe_not_current")
        if cleanup_at is None or not stale or scheduler.retained_account_count:
            raise ValueError("targeted_probe_cleanup_or_stale_reference_failure")
        atomic_status(
            status_path, "probe", state="complete",
            seconds=round(seconds, 3), gate=seconds <= 10,
            commit_to_built=round(result["probe"]["commit_to_built_seconds"], 3),
            built_to_validated=round(result["probe"]["built_to_validated_seconds"], 3),
            validated_to_done=round(result["probe"]["validated_to_done_seconds"], 3),
        )
        result["profiling"] = args.profile_output is not None
        result["qualifies_latency"] = args.profile_output is None
        result["complete"] = True
    finally:
        observer_stop.set()
        if thread is not None:
            thread.join(timeout=10)
        try:
            samples.append(sample())
        except BaseException as error:
            sample_errors.append(type(error).__name__)
        result["host"] = {
            "samples": samples,
            "errors": sample_errors,
            "observer_joined": thread is None or not thread.is_alive(),
            "nonempty_samples": sum(bool(item["other_benchmark_jobs"]) for item in samples),
        }
        resources.close()
        result["scheduler_closed"] = await scheduler.close(timeout=10)
        result["detached_workers"] = scheduler.detached_worker_count
        work.close()
        result["source_unchanged"] = q.source_context(args.source_root) == source
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return result


def print_seed_summary(seed: dict) -> None:
    print(
        "ready-seed"
        f" source={seed['source_revision'][:12]}"
        f" recovery={seed['recovery_seconds']:.3f}s"
        f" verification={seed['verification_seconds']:.3f}s"
        f" path={seed.get('path', '')}",
        flush=True,
    )


def print_probe_summary(result: dict, receipt: Path) -> None:
    probe = result["probe"]
    print(
        "targeted-result"
        f" source={result['source_revision'][:12]}"
        f" total={probe['seconds']:.3f}s"
        f" gate={'PASS' if probe['ten_second_gate'] else 'FAIL'}"
        f" commit_to_built={probe['commit_to_built_seconds']:.3f}s"
        f" built_to_validated={probe['built_to_validated_seconds']:.3f}s"
        f" validated_to_done={probe['validated_to_done_seconds']:.3f}s"
        f" prepare={result['question_prepare_seconds']:.3f}s"
        f" reconcile={result['initial_reconciliation_seconds']:.3f}s"
        f" receipt_sha256={file_sha256(receipt)}",
        flush=True,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source-root", type=Path, required=True)
    value.add_argument("--expected-sha", required=True)
    value.add_argument("--gnupg-home", type=Path)
    value.add_argument("--owner-lock", type=Path, required=True)
    value.add_argument("--status", type=Path, required=True)
    action = value.add_subparsers(dest="action", required=True)

    prepare = action.add_parser("prepare-seed")
    prepare.add_argument("--base-seed", type=Path, required=True)
    prepare.add_argument("--ready-seed", type=Path, required=True)

    run = action.add_parser("run")
    run.add_argument("--ready-seed", type=Path, required=True)
    run.add_argument("--result", type=Path, required=True)
    run.add_argument("--workdir", type=Path)
    run.add_argument("--case", choices=("dominant", "ordinary"), default="dominant")
    run.add_argument("--idle-seconds", type=float)
    run.add_argument("--probe-timeout", type=float, default=120)
    run.add_argument("--keep-workdir", action="store_true")
    run.add_argument(
        "--profile-output", type=Path,
        help="Write non-qualifying thread-local activation attribution records.",
    )
    run.add_argument("--job-memory-limit", type=int, default=DEFAULT_JOB_MEMORY)
    run.add_argument("--host-reserve", type=int, default=DEFAULT_HOST_RESERVE)
    run.add_argument("--preflight-memory", type=int, default=DEFAULT_PREFLIGHT)
    return value


def main() -> int:
    args = parser().parse_args()
    args.source_root = args.source_root.resolve()
    q = qualification(args.source_root)
    source = validate_source(q, args.source_root, args.expected_sha, args.gnupg_home)
    manifest = q.read_json(args.source_root / "docs/analytics/acceptance-manifest.json")
    runtime = q.runtime_context()
    with q.owner_lock(args.owner_lock):
        if args.action == "prepare-seed":
            atomic_status(args.status, "seed", state="starting", source=source["revision"])
            seed = asyncio.run(prepare_seed(args, q, source, manifest, runtime, args.status))
            seed["path"] = str(args.ready_seed.resolve())
            print_seed_summary(seed)
            return 0

        if args.result.exists():
            raise FileExistsError(args.result)
        if args.profile_output is not None and args.profile_output.exists():
            raise FileExistsError(args.profile_output)
        concurrent = _other_benchmark_jobs()
        if concurrent:
            raise RuntimeError("concurrent_benchmark_process_detected:" + str(concurrent))
        guard = MemoryGuard(
            job_limit=args.job_memory_limit,
            host_reserve=args.host_reserve,
            preflight_minimum=args.preflight_memory,
        )
        result = None
        failure = None
        try:
            atomic_status(args.status, "run", state="starting", source=source["revision"])
            result = asyncio.run(run_probe(args, q, source, manifest, runtime, args.status, guard))
        except BaseException as error:
            failure = error
        finally:
            memory = guard.close()
        if result is None:
            result = {
                "schema": RECEIPT_SCHEMA, "complete": False,
                "source_revision": source["revision"], "source": source,
                "manifest_sha256": q.digest(manifest), "runtime_sha256": q.digest(runtime),
                "runner_sha256": file_sha256(Path(__file__).resolve()),
                "error_type": type(failure).__name__ if failure is not None else "RuntimeError",
                "error": str(failure) if failure is not None else "targeted_run_failed_before_receipt",
            }
        result["memory_guard"] = memory
        q.write_once(args.result, result)
        receipt_sha = file_sha256(args.result)
        if failure is not None:
            atomic_status(
                args.status, "failed", receipt=str(args.result),
                receipt_sha256=receipt_sha, error_type=type(failure).__name__,
            )
            print(
                f"targeted-failure source={source['revision'][:12]} "
                f"error={type(failure).__name__} receipt_sha256={receipt_sha}",
                flush=True,
            )
            return 2
        atomic_status(
            args.status, "complete", receipt=str(args.result), receipt_sha256=receipt_sha,
        )
        print_probe_summary(result, args.result)
        return 0 if result["probe"]["ten_second_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
