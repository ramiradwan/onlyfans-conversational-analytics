"""Closure mode for the existing analytics baseline command."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from uuid import uuid4

from tools import analytics_qualification as q


def preflight(directory: Path, session: str) -> None:
    q.write_once(directory / "preflight" / (session + ".json"), {
        "observed": q.stamp(), "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "free_disk_bytes": shutil.disk_usage(directory).free,
        "pyinstaller_available": importlib.util.find_spec("PyInstaller") is not None,
        "inno_compiler": shutil.which("iscc"), "windows": windows_preflight(),
        "profiles": "BLOCKED", "package": "BLOCKED",
        "reason": "Qualification requires measured Windows profiles and immutable package inputs. Source diagnostics do not establish packaged behavior.",
    })


from tools.analytics_qualification_process import supervise


def finish(attempt: Path, context: dict, job: str, result: dict) -> None:
    q.write_once(attempt / "result.json", dict(result, job=job,
        context_sha256=q.digest(context), finished=q.stamp()))


def run_regressions(root: Path, directory: Path, context: dict, session: str, manifest: dict) -> None:
    attempt = q.begin_attempt(directory, context, session, "regression")
    command = [sys.executable, str(root / "tools/qualify_analytics_baseline.py"),
               "--output", str(attempt / "regression")]
    result = {}
    try:
        result = supervise(command, root, attempt, manifest["limits"]["whole_worker_seconds"])
        if result["status"] != "BLOCKED":
            report = attempt / "regression/report.json"
            payload = q.read_json(report) if report.exists() else {"runs": []}
            result.update(payload=payload, payload_sha256=q.digest(payload),
                complete=payload.get("complete") is True,
                source_after_sha256=q.digest(q.source_context(root)))
            attachments = []
            for file in sorted((attempt / "regression").glob("*")):
                if file.is_file() and file.suffix in {".json", ".xml", ".log"}:
                    attachments.append(dict(q.attach(attempt, file), name=file.name))
            result["attachments"] = attachments
            if payload.get("passed") is not True:
                result["status"] = "FAIL"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result.update(status="FAIL", complete=False, reporting_error=type(error).__name__,
                      reason=result.get("reason") or type(error).__name__)
    finish(attempt, context, "regression", result)


def run_source(root, directory, context, session, manifest, args):
    kind, profile = args.run_source, getattr(args, "profile", "reference-windows-16g")
    job = (f"mutation/{profile}/{args.messages}" if kind == "matrix" else
           f"questions/{profile}/{args.case}/{args.state}" if kind == "questions" else
           f"visibility/{profile}/{args.repeat}")
    attempt = q.begin_attempt(directory, context, session, job)
    hardware, hardware_evidence = None, None
    subject_root = (args.subject_root or root).resolve()
    subject = q.source_context(subject_root)
    config = {"subject_directory": str(subject_root), "subject_files": subject["files"],
        "subject_sha256": q.digest(subject), "subject": subject,
        "manifest": manifest, "mode": kind, "messages": args.messages,
        "case": args.case, "state": args.state, "repeat": args.repeat,
        "continue_after_visibility_failure": getattr(args, "continue_after_visibility_failure", False),
        "known_kinds": args.known_synthetic_kinds, "profile_updates": getattr(args, "profile_updates", False), "output": str(attempt / "collector"),
        "data": str(attempt / "collector/data"), "entry_point": str(root / "tools/qualify_analytics_baseline.py"),
        "job": job, "profile": profile, "hardware": hardware,
        "semantic_questions": getattr(args, "run_questions", False)}
    prepared_protocol = manifest["questions"].get("preparation", {}).get("protocol")
    if kind == "questions" and prepared_protocol == "verified-question-inputs.v1":
        config["question_baselines"] = str(directory / "question-baselines")
    if config["semantic_questions"]:
        from tools.analytics_qualification_hardware_evidence import HardwareEvidence, workload_paths
        from tools.analytics_qualification_tracks import check_profile
        try:
            if subject_root != root.resolve():
                raise ValueError("semantic_question_source_directory_mismatch")
            paths = workload_paths(root, attempt, data=config["data"])
            hardware_evidence = HardwareEvidence(attempt, context, manifest, profile, paths,
                                                 getattr(args, "hardware_handoff", None))
            config["hardware"] = hardware_evidence.hardware
            config["hardware_runtime"] = hardware_evidence.runtime_paths
            errors = check_profile(manifest, profile, config["hardware"])
            if errors:
                raise ValueError(";".join(errors))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            finish(attempt, context, job, {"status": "BLOCKED", "worker_started": False,
                "exit_code": None, "reason": str(error)})
            return
    execution_schedule = None
    worker_limit = manifest["limits"]["whole_worker_seconds"]
    if kind == "visibility" and "visibility_execution" in manifest:
        execution_schedule = dict(manifest["visibility_execution"],
                                  directory=str(attempt / "collector/execution"))
        config["execution_schedule"] = execution_schedule
        worker_limit = manifest["visibility_execution"]["maximum_worker_seconds"]
    q.write_once(attempt / "worker-input.json", config)
    command = [sys.executable, config["entry_point"], "--collector-worker", str(attempt / "worker-input.json")]
    result = {}
    try:
        if hardware_evidence:
            hardware_evidence.before_worker()
        result = supervise(command, root, attempt, worker_limit, limits=manifest["limits"],
                           execution_schedule=execution_schedule)
        if result["worker_started"]:
            payload_path = attempt / "collector/payload.json"
            payload = q.read_json(payload_path) if payload_path.exists() else {"complete": False}
            result.update(payload=payload, payload_sha256=q.digest(payload),
                complete=payload.get("complete") is True, source_after_sha256=q.digest(q.source_context(root)),
                subject=subject, subject_after_sha256=q.digest(q.source_context(subject_root)))
            attachments = []
            for file in sorted((attempt / "collector").rglob("*")):
                if (file.is_file() and file.suffix in {".json", ".log"}
                        and "data" not in file.relative_to(attempt / "collector").parts):
                    attachments.append(dict(q.attach(attempt, file), name=file.relative_to(attempt / "collector").as_posix()))
            attachments.append(dict(q.attach(attempt, attempt / "worker-input.json"), name="worker-input.json"))
            attachments.append(dict(q.attach(attempt, attempt / "worker.log"), name="worker.log"))
            result["attachments"] = attachments
            if not result["complete"]:
                result["status"] = "FAIL"
            result["diagnostic_errors"] = q.check_payload(manifest, context, job, payload)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result.update(status="FAIL", complete=False, reporting_error=type(error).__name__,
                      reason=result.get("reason") or type(error).__name__)
    if hardware_evidence:
        try:
            if result.get("worker_started"):
                hardware_evidence.after_worker(result)
                result["source_after_sha256"] = q.digest(q.source_context(root))
                result["subject_after_sha256"] = q.digest(q.source_context(subject_root))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            result.update(status="FAIL", complete=False, reason=result.get("reason") or str(error))
        result.setdefault("attachments", []).extend(hardware_evidence.references)
    finish(attempt, context, job, result)


def run_ci(root, directory, context, session, manifest, review_path):
    attempt = q.begin_attempt(directory, context, session, "source-ci")
    if shutil.which("gh") is None:
        finish(attempt, context, "source-ci", {"status": "BLOCKED", "worker_started": False,
            "exit_code": None, "reason": "gh_command_unavailable"})
        return
    result = {}
    try:
        config = {"root": str(root), "manifest": manifest, "source": context["source"],
                  "review": q.read_json(review_path), "output": str(attempt / "ci.json")}
        q.write_once(attempt / "ci-input.json", config)
        result = supervise([sys.executable, str(root / "tools/qualify_analytics_baseline.py"),
            "--ci-worker", str(attempt / "ci-input.json")], root, attempt, manifest["limits"]["whole_worker_seconds"])
        payload = q.read_json(attempt / "ci.json") if (attempt / "ci.json").exists() else {}
        result.update(payload=payload, payload_sha256=q.digest(payload), complete=payload.get("complete") is True,
            source_after_sha256=q.digest(q.source_context(root)), attachments=[])
        if payload:
            result["attachments"] = [dict(q.attach(attempt, attempt / "ci.json"), name="ci.json")]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result.update(status="FAIL", complete=False, reporting_error=type(error).__name__,
                      reason=result.get("reason") or type(error).__name__)
    finish(attempt, context, "source-ci", result)


def main(root: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closure", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify", action="store_true")
    action.add_argument("--campaign-template", type=Path,
                        help="Write an exact-source template with every missing prerequisite explicitly null; no jobs run.")
    action.add_argument("--run-campaign", action="store_true",
                        help="Run ready mandatory jobs on the current profile; blocked prerequisites remain explicit.")
    action.add_argument("--run-job", help="Select exactly one job from the authoritative manifest.")
    action.add_argument("--campaign-plan", action="store_true", help="Print all required routes without starting a worker.")
    parser.add_argument("--campaign-inputs", type=Path, help="Exact-source job prerequisite references, no commands or credentials.")
    parser.add_argument("--preflight-campaign", action="store_true", help="Check every configured route without starting jobs.")
    action.add_argument("--run-regressions", action="store_true")
    action.add_argument("--run-ci", action="store_true")
    parser.add_argument("--review-record", type=Path)
    action.add_argument("--run-source", choices=("matrix", "questions", "visibility"))
    action.add_argument("--run-questions", action="store_true")
    action.add_argument("--run-package", choices=("package", "matrix", "visibility"))
    parser.add_argument("--package-inputs", type=Path)
    parser.add_argument("--hardware-handoff", type=Path)
    parser.add_argument("--profile", choices=("reference-windows-16g", "constrained-windows-8g"), default="reference-windows-16g")
    parser.add_argument("--subject-root", type=Path)
    parser.add_argument("--messages", type=int, choices=(10000, 100000), default=100000)
    parser.add_argument("--case", choices=("populated", "empty", "tied_time", "generation_bound_pagination"), default="empty")
    parser.add_argument("--state", choices=("fresh", "idle", "mutated"), default="fresh")
    parser.add_argument("--repeat", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--known-synthetic-kinds", action="store_true")
    parser.add_argument("--profile-updates", action="store_true", help="Attribute source updates; this run cannot qualify latency")
    parser.add_argument("--owner-lock", type=Path)
    parser.add_argument("--continue-after-visibility-failure", action="store_true",
                        help="Diagnostic only: finish later visibility cases after a failure; never qualifies.")
    args = parser.parse_args()
    if (args.run_campaign or args.run_job or args.preflight_campaign) and args.campaign_inputs is None:
        parser.error("Campaign execution/preflight requires --campaign-inputs")
    if args.preflight_campaign and (args.run_campaign or args.run_job):
        parser.error("Preflight cannot also execute a campaign")
    if args.continue_after_visibility_failure and args.run_source != "visibility":
        parser.error("--continue-after-visibility-failure requires --run-source visibility")
    if args.run_ci and args.review_record is None:
        parser.error("--run-ci requires --review-record bound to the exact clean source")
    if args.run_questions:
        if args.subject_root is not None or args.profile_updates:
            parser.error("Semantic question qualification requires the final source without profiling")
        args.run_source, args.known_synthetic_kinds = "questions", True
    if args.run_package and args.package_inputs is None:
        parser.error("--run-package requires --package-inputs")
    directory = args.output.resolve()
    manifest = q.read_json(root / "docs/analytics/acceptance-manifest.json")
    try:
        source = q.source_context(root)
        if args.campaign_plan:
            from tools.analytics_qualification_campaign import describe
            print(q.encoded(describe(manifest)).decode(), end="")
            return 0
        if args.verify:
            result = q.verify(directory, manifest, current_source=source)
            print(q.encoded(result).decode(), end="")
            return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result["status"]]
        lock = args.owner_lock or Path.home() / ".ofca-qualification" / "analytics-owner.lock"
        with q.owner_lock(lock):
            runtime = q.runtime_context()
            if args.resume:
                context = q.read_json(directory / "context.json")
                if (context["manifest_sha256"] != q.digest(manifest)
                        or q.digest(context["source"]) != q.digest(source)
                        or q.digest(context["runtime"]) != q.digest(runtime)):
                    parser.error("Resume requires identical source, manifest and runtime; start a new evidence directory.")
            else:
                artifacts = {}
                if args.package_inputs is not None:
                    from tools.analytics_qualification_packaged import artifact_context
                    artifacts = artifact_context(q.read_json(args.package_inputs))
                context = q.initialize(directory, manifest, source, runtime, artifacts)
            if args.campaign_template:
                from tools.analytics_qualification_campaign import input_template
                q.write_once(args.campaign_template.resolve(), input_template(context, manifest))
                print(q.encoded({"status":"TEMPLATE_ONLY","qualification_credit":0,
                      "required_jobs":len(q.required_jobs(manifest)),"path":str(args.campaign_template.resolve())}).decode(),end="")
                return 0
            if args.run_campaign or args.run_job or args.preflight_campaign:
                from tools.analytics_qualification_campaign import checked_inputs, prerequisites, plan, run_ready
                config = checked_inputs(q.read_json(args.campaign_inputs), context, manifest)
                if args.preflight_campaign:
                    blocked = {item["job"]: reasons for item in plan(manifest)
                        if (reasons := prerequisites(manifest, context, item["job"], config["jobs"][item["job"]], active_profile=args.profile))}
                    result = {"status": "BLOCKED" if blocked else "READY_FOR_PUBLIC_COLLECTORS",
                              "required_jobs": len(q.required_jobs(manifest)), "blocked": blocked,
                              "qualification_credit": 0, "actual_collector_prerequisites_still_required": True}
                else:
                    result = run_ready(root, directory, context, manifest, config,
                                       active_profile=args.profile, selected_job=args.run_job)
                q.write_once(directory / "campaign-reports" / (uuid4().hex + ".json"), result)
                print(q.encoded(result).decode(), end="")
                return 0 if result["status"] in {"PASS", "READY_FOR_PUBLIC_COLLECTORS"} else 1 if result["status"] == "FAIL" else 2
            identity = q.session(directory, context)
            if not args.resume:
                preflight(directory, identity)
            if args.run_ci:
                run_ci(root, directory, context, identity, manifest, args.review_record)
            if args.run_regressions:
                run_regressions(root, directory, context, identity, manifest)
            if args.run_source:
                run_source(root, directory, context, identity, manifest, args)
            if args.run_package:
                from tools.analytics_qualification_packaged import run
                run(root, directory, context, identity, manifest, args)
            result = q.verify(directory, manifest, current_source=q.source_context(root))
            q.write_once(directory / "verdicts" / (identity + ".json"), result)
            print(q.encoded(result).decode(), end="")
            return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result["status"]]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(q.encoded({"status": "FAIL", "reason": str(error)}).decode(), end="")
        return 1


def windows_preflight() -> dict:
    """Read authorized host facts; never elevate or modify guest permissions."""
    executable = shutil.which("powershell.exe")
    if executable is None:
        return {"status": "BLOCKED", "reason": "windows_shell_unavailable"}
    command = r'''
$ErrorActionPreference = 'Stop'
$r = [ordered]@{}
try {
  $r.os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,TotalVisibleMemorySize,FreePhysicalMemory
  $r.cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors)
} catch { $r.hardware_error = $_.FullyQualifiedErrorId }
try {
  $r.guests = @(Get-VM -ComputerName localhost | Select-Object Name,State,MemoryStartup,ProcessorCount)
  $r.guest_access = 'available'
} catch { $r.guest_access = 'BLOCKED'; $r.guest_error = $_.FullyQualifiedErrorId }
$r.packaging_commands = @(Get-Command iscc,pyinstaller -ErrorAction SilentlyContinue | Select-Object Name,Source)
$r | ConvertTo-Json -Depth 6 -Compress
'''
    try:
        process = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=20, check=False)
        return {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr,
                "status": "BLOCKED", "reason": "Host discovery is not declared-profile qualification."}
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "BLOCKED", "reason": type(error).__name__}
