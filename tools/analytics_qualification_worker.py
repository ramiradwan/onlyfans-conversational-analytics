"""Internal source collector for the existing analytics qualification entry point."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import gc
import shutil
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from tools import analytics_qualification as q
from tools.analytics_qualification_fixture import Journal, Workload
from tools.analytics_qualification_workloads import direct, matrix, scheduled
from tools.analytics_qualification_questions import questions
from tools.analytics_qualification_execution import mark_state
from tools.analytics_qualification_progress import CollectorProgress


def subject_matches(config):
    root = Path(config["subject_directory"])
    return all((root / name).is_file() and q.file_digest(root / name) == sha
               for name, sha in config["subject_files"].items())


def child(config, directory, mode):
    command_config = dict(config, mode=mode, output=str(directory))
    directory.mkdir()
    path = directory / "input.json"
    q.write_once(path, command_config)
    command = [sys.executable, config["entry_point"], "--collector-worker", str(path)]
    with (directory / "child.log").open("x", encoding="utf-8") as log:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, check=False)
    payload = q.read_json(directory / "payload.json")
    payload["child_exit_code"] = result.returncode
    return payload


def stop_after_verified_probe(work, journal, config, report, cases, probe):
    """Stop only after scheduled() has saved its independent verification."""
    if (not work.manifest["visibility"].get("fail_fast_after_verified_probe", False)
            or config.get("continue_after_visibility_failure", False)):
        return False
    profile = config["job"].split("/")[1]
    reasons = q.check_visibility_probe(work.manifest, profile, probe)
    if not reasons:
        return False
    remaining = cases[cases.index(probe["case"]) + 1:]
    stop = dict(case=probe["case"], reasons=reasons, at=time.monotonic(),
                verification_completed=probe.get("independent_rebuild_equal") is True
                    and probe.get("persisted_content_revalidated") is True)
    report.update(complete=False, stopped_after_verified_failure=stop,
                  unexecuted_cases=remaining)
    journal.save("visibility-stop", dict(stop, unexecuted_cases=remaining))
    return True


async def visibility(work, journal, config, instance, *, restarted=False):
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1, queue_capacity=64,
                                            reconciliation_interval=30)
    report = {"initial_messages": work.size, "probes": [], "complete": False,
              "runtime_processes": [instance]}
    cases = work.manifest["visibility"]["process_cases"][config["repeat"]]
    attempted = []
    progress = getattr(work, "qualification_progress", None)
    def step(name):
        return progress.phase(name) if progress else nullcontext()
    try:
        if not restarted:
            await direct(work, journal, resources, "cold")
        with step("restart.scheduler_readiness" if restarted else "scheduler.readiness"):
            resources.start()
            await scheduler.start(recover=True)
            if restarted:
                state = await scheduler.wait(work.account)
                if state.availability != AvailabilityStatus.AVAILABLE:
                    raise ValueError("restart_not_ready")
        if restarted:
            with step("restart.reference_capture"):
                reference = await asyncio.to_thread(work.capture_current_reference)
                journal.save("restart-reference", reference)
        for case in cases[-1:] if restarted else cases[:-1]:
            attempted.append(case)
            state, thread = case.split("/")
            if not restarted:
                mark_state(config, state, instance)
            if state == "rebuilt":
                await direct(work, journal, resources, "unchanged_rebuild")
            if state == "idle":
                with step("idle.wait"):
                    started = time.monotonic()
                    await asyncio.sleep(work.manifest["visibility"]["idle_seconds"])
                    journal.save("idle", {"seconds": time.monotonic() - started})
            probe = await scheduled(work, journal, resources, scheduler, "one_committed_message",
                lambda: work.add(0 if thread == "dominant" else 1, "visibility-" + case.replace("/", "-")), case=case)
            probe["process_instance"] = instance
            report["probes"].append(probe)
            if stop_after_verified_probe(work, journal, config, report, cases, probe):
                break
        else:
            report["complete"] = True
    except Exception as error:
        report.update(complete=False, error_type=type(error).__name__, error=str(error),
                      unexecuted_cases=[case for case in (cases[-1:] if restarted else cases)
                                        if case not in attempted])
        journal.save("failure", {"error_type": type(error).__name__, "error": str(error)})
    finally:
        # A resource or journal failure must not skip the owned scheduler shutdown.
        try:
            resources.close()
        finally:
            try:
                report["scheduler_closed"] = await scheduler.close(timeout=10)
            finally:
                report["detached_workers"] = scheduler.detached_worker_count
                report["backlog"] = scheduler.retained_account_count
                if (report.get("scheduler_closed") is not True
                        or report["detached_workers"] or report["backlog"]):
                    report["complete"] = False
                try:
                    journal.save("visibility", report)
                finally:
                    work.close()
    return report


def collect_visibility(work, journal, config, instance, *, restarted=False):
    report = asyncio.run(visibility(work, journal, config, instance, restarted=restarted))
    if restarted or not report.get("complete"):
        return report
    if not report["scheduler_closed"] or report["detached_workers"]:
        raise ValueError("cannot_restart_with_live_workers")
    mark_state(config, "restarted", instance)
    restart = child(config, Path(config["output"]) / "restarted-process", "visibility-restarted")
    report["probes"].extend(restart["probes"])
    report["runtime_processes"].extend(restart["runtime_processes"])
    report["complete"] = report["complete"] and restart["complete"]
    report["restart_scheduler_closed"] = restart["scheduler_closed"]
    report["restart_detached_workers"] = restart["detached_workers"]
    report["restart_backlog"] = restart["backlog"]
    for key in ("stopped_after_verified_failure", "unexecuted_cases", "error", "error_type"):
        if key in restart:
            report[key] = restart[key]
    return report


def collect(config):
    output, manifest = Path(config["output"]), config["manifest"]
    output.mkdir(parents=True, exist_ok=True)
    configured_at = time.monotonic()
    instance = str(os.getpid()) + ":" + uuid4().hex
    journal = Journal(output / "events", instance)
    progress = CollectorProgress(output, instance)
    process = {"instance": instance, "pid": os.getpid(), "started": q.stamp(),
        "supervisor_instance": os.environ.get("OFCA_QUALIFICATION_PROCESS"),
        "runtime": q.runtime_context(), "subject_sha256": config["subject_sha256"]}
    q.write_once(output / "process.json", process)
    if not subject_matches(config):
        raise ValueError("subject_source_mismatch_before_start")
    sys.path.insert(0, config["subject_directory"])
    import tests.conftest  # Only isolated synthetic stores and test keys.
    mode = config["mode"]
    if mode == "visibility":
        mark_state(config, "cold", instance)
    data = Path(config["data"])
    report = {"complete": False, "initial_messages": config["messages"]}
    work = None
    try:
        with progress.phase("restart.storage_initialization" if mode == "visibility-restarted" else "fixture.initialization"):
            work = Workload(data, manifest, config["messages"],
                reopen=mode in {"questions-child", "visibility-restarted"},
                question_case=config["case"] if mode in {"questions", "questions-child"} else None,
                known_kinds=config["known_kinds"])
        work.qualification_progress = progress
        if config.get("profile_updates"):
            from tools.analytics_qualification_profile import install
            install(work, output)
        q.write_once(output / "fixture.json", {"definition": manifest["fixture"],
            "manifest_sha256": q.digest(manifest), "source_counts": work.counts(),
            "kind_adapter": "known_synthetic_kinds" if config["known_kinds"] else "production_unknown_kinds",
            "input_path": "direct_synthetic_database_fixture", "question_case": config["case"]})
        if mode == "matrix":
            report = asyncio.run(matrix(work, journal))
        elif mode == "questions-child":
            report = asyncio.run(questions(work, journal, instance, config["case"], config["state"], configured_at))
        elif mode == "questions":
            candidate = work.f.pipeline.build_candidate(work.account, force=True)
            work.f.pipeline.publish_candidate(candidate)
            preparation = work.verify()
            work.close()
            work = None
            gc.collect()
            preparation_seconds = time.monotonic() - configured_at
            journal.save("prepared", {"seconds": preparation_seconds, **preparation})
            report = child(config, output / "fresh-process", "questions-child")
            report.update(preparation_seconds=preparation_seconds, preparation=preparation)
        elif mode in {"visibility", "visibility-restarted"}:
            report = collect_visibility(work, journal, config, instance,
                                        restarted=mode == "visibility-restarted")
        else:
            raise ValueError("unknown_collector")
    except BaseException as error:
        report.update(complete=False, error_type=type(error).__name__, error=str(error))
        journal.save("failure", {"error_type": type(error).__name__, "error": str(error)})
    finally:
        if work is not None:
            work.close()
        report.update(execution="source_diagnostic", profiling=config.get("profile_updates", False), supervisor_instance=process["supervisor_instance"],
            collector_process=process, subject_sha256=config["subject_sha256"],
            subject_unchanged=subject_matches(config), manifest_sha256=q.digest(manifest),
            fixture_mode="known_synthetic_kinds" if config["known_kinds"] else "production_unknown_kinds",
            clock=manifest["fixture"]["evaluation_clock"],
            continue_after_visibility_failure=config.get("continue_after_visibility_failure", False))
        q.write_once(output / "payload.json", report)
        progress.record("collector", "complete" if report.get("complete") else "failed",
                        unexecuted_cases=report.get("unexecuted_cases", []))
    return report


def main(input_path: str) -> int:
    if not os.environ.get("OFCA_QUALIFICATION_PROCESS"):
        raise ValueError("collector_requires_owned_supervisor")
    config = q.read_json(Path(input_path))
    report = collect(config)
    # Child samples remain available even when the product question failed.
    if not report.get("complete") or report.get("subject_unchanged") is not True:
        return 1
    if config["mode"] in {"questions-child", "visibility-restarted"}:
        return 0
    errors = q.check_payload(config["manifest"], {}, config["job"], report)
    return 1 if errors else 0
