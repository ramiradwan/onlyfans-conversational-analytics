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
        "reason": "Declared Windows profiles and the packaged UI/ingestion adapter are unavailable. Source collectors cannot qualify them.",
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
    kind, profile = args.run_source, "reference-windows-16g"
    job = (f"mutation/{profile}/{args.messages}" if kind == "matrix" else
           f"questions/{profile}/{args.case}/{args.state}" if kind == "questions" else
           f"visibility/{profile}/{args.repeat}")
    attempt = q.begin_attempt(directory, context, session, job)
    subject_root = (args.subject_root or root).resolve()
    subject = q.source_context(subject_root)
    config = {"subject_directory": str(subject_root), "subject_files": subject["files"],
        "subject_sha256": q.digest(subject), "subject": subject,
        "manifest": manifest, "mode": kind, "messages": args.messages,
        "case": args.case, "state": args.state, "repeat": args.repeat,
        "known_kinds": args.known_synthetic_kinds, "output": str(attempt / "collector"),
        "data": str(attempt / "collector/data"), "entry_point": str(root / "tools/qualify_analytics_baseline.py"),
        "job": job}
    q.write_once(attempt / "worker-input.json", config)
    command = [sys.executable, config["entry_point"], "--collector-worker", str(attempt / "worker-input.json")]
    result = {}
    try:
        result = supervise(command, root, attempt, manifest["limits"]["whole_worker_seconds"], limits=manifest["limits"])
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
    action.add_argument("--run-regressions", action="store_true")
    action.add_argument("--run-ci", action="store_true")
    parser.add_argument("--review-record", type=Path)
    action.add_argument("--run-source", choices=("matrix", "questions", "visibility"))
    parser.add_argument("--subject-root", type=Path)
    parser.add_argument("--messages", type=int, choices=(10000, 100000), default=100000)
    parser.add_argument("--case", choices=("populated", "empty", "tied_time", "generation_bound_pagination"), default="empty")
    parser.add_argument("--state", choices=("fresh", "idle", "mutated"), default="fresh")
    parser.add_argument("--repeat", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--known-synthetic-kinds", action="store_true")
    parser.add_argument("--owner-lock", type=Path)
    args = parser.parse_args()
    if args.run_ci and args.review_record is None:
        parser.error("--run-ci requires --review-record bound to the exact clean source")
    directory = args.output.resolve()
    manifest = q.read_json(root / "docs/analytics/acceptance-manifest.json")
    try:
        source = q.source_context(root)
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
                context = q.initialize(directory, manifest, source, runtime)
            identity = q.session(directory, context)
            if not args.resume:
                preflight(directory, identity)
            if args.run_ci:
                run_ci(root, directory, context, identity, manifest, args.review_record)
            if args.run_regressions:
                run_regressions(root, directory, context, identity, manifest)
            if args.run_source:
                run_source(root, directory, context, identity, manifest, args)
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
