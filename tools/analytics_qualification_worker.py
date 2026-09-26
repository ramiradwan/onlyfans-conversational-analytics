"""Internal source collector for the existing analytics qualification entry point."""
from __future__ import annotations

import asyncio
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


async def visibility(work, journal, config, instance, *, restarted=False):
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus
    resources = QuestionResources(work.f.source, work.f.pipeline, clock=lambda: work.clock)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1, queue_capacity=64,
                                            reconciliation_interval=30)
    report = {"initial_messages": work.size, "probes": [], "complete": False,
              "runtime_processes": [instance]}
    cases = work.manifest["visibility"]["process_cases"][config["repeat"]]
    try:
        if not restarted:
            await direct(work, journal, resources, "cold")
        resources.start()
        await scheduler.start(recover=restarted)
        if restarted:
            state = await scheduler.wait(work.account)
            if state.availability != AvailabilityStatus.AVAILABLE:
                raise ValueError("restart_not_ready")
        for case in cases[-1:] if restarted else cases[:-1]:
            state, thread = case.split("/")
            if state == "rebuilt":
                await direct(work, journal, resources, "unchanged_rebuild")
            if state == "idle":
                started = time.monotonic()
                await asyncio.sleep(work.manifest["visibility"]["idle_seconds"])
                journal.save("idle", {"seconds": time.monotonic() - started})
            probe = await scheduled(work, journal, resources, scheduler, "one_committed_message",
                lambda: work.add(0 if thread == "dominant" else 1, "visibility-" + case.replace("/", "-")), case=case)
            probe["process_instance"] = instance
            report["probes"].append(probe)
        report["complete"] = True
    finally:
        resources.close()
        report["scheduler_closed"] = await scheduler.close(timeout=10)
        report["detached_workers"] = scheduler.detached_worker_count
        report["backlog"] = scheduler.retained_account_count
        journal.save("visibility", report)
        work.close()
    return report


def collect(config):
    output, manifest = Path(config["output"]), config["manifest"]
    output.mkdir(parents=True, exist_ok=True)
    configured_at = time.monotonic()
    instance = str(os.getpid()) + ":" + uuid4().hex
    journal = Journal(output / "events", instance)
    process = {"instance": instance, "pid": os.getpid(), "started": q.stamp(),
        "supervisor_instance": os.environ.get("OFCA_QUALIFICATION_PROCESS"),
        "runtime": q.runtime_context(), "subject_sha256": config["subject_sha256"]}
    q.write_once(output / "process.json", process)
    if not subject_matches(config):
        raise ValueError("subject_source_mismatch_before_start")
    sys.path.insert(0, config["subject_directory"])
    import tests.conftest  # Only isolated synthetic stores and test keys.
    mode = config["mode"]
    data = Path(config["data"])
    report = {"complete": False, "initial_messages": config["messages"]}
    work = None
    try:
        work = Workload(data, manifest, config["messages"],
            reopen=mode in {"questions-child", "visibility-restarted"},
            question_case=config["case"] if mode in {"questions", "questions-child"} else None,
            known_kinds=config["known_kinds"])
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
            report = asyncio.run(visibility(work, journal, config, instance,
                                           restarted=mode == "visibility-restarted"))
            if mode == "visibility":
                if not report["scheduler_closed"] or report["detached_workers"]:
                    raise ValueError("cannot_restart_with_live_workers")
                restarted = child(config, output / "restarted-process", "visibility-restarted")
                report["probes"].extend(restarted["probes"])
                report["runtime_processes"].extend(restarted["runtime_processes"])
                report["complete"] = report["complete"] and restarted["complete"]
                report["restart_scheduler_closed"] = restarted["scheduler_closed"]
                report["restart_detached_workers"] = restarted["detached_workers"]
                report["restart_backlog"] = restarted["backlog"]
        else:
            raise ValueError("unknown_collector")
    except BaseException as error:
        report.update(complete=False, error_type=type(error).__name__, error=str(error))
        journal.save("failure", {"error_type": type(error).__name__, "error": str(error)})
    finally:
        if work is not None:
            work.close()
        report.update(execution="source_diagnostic", supervisor_instance=process["supervisor_instance"],
            collector_process=process, subject_sha256=config["subject_sha256"],
            subject_unchanged=subject_matches(config), manifest_sha256=q.digest(manifest),
            fixture_mode="known_synthetic_kinds" if config["known_kinds"] else "production_unknown_kinds",
            clock=manifest["fixture"]["evaluation_clock"])
        q.write_once(output / "payload.json", report)
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
