"""Run the default backend selection with isolated workers and a serial pass."""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXPRESSION = ("not slow and not windows_production and not stateful_tier_a "
              "and not stateful_tier_b and not stateful_agent_tier_a")
EXCLUSIONS = ["--ignore=tests/test_packaged_runtime.py", "--ignore=tests/test_packaging_smoke.py",
              "--deselect=tests/test_launcher.py::test_provisioning_control_client_connects_to_loopback_with_allowed_host"]


def junit_nodeid(case):
    parts = case.get("classname", "").split(".")
    boundary = next((i for i, part in enumerate(parts) if part[:1].isupper()), len(parts))
    return "/".join(parts[:boundary]) + ".py::" + "::".join(parts[boundary:] + [case.get("name", "")])


def junit_outcomes(path):
    result = {}
    priority = {"passed": 0, "skipped": 1, "failure": 2, "error": 3}
    for case in ET.parse(path).iter("testcase"):
        node = junit_nodeid(case)
        outcome = next((name for name in ("error", "failure", "skipped") if case.find(name) is not None), "passed")
        if priority[outcome] >= priority.get(result.get(node), -1):
            result[node] = outcome
    return result


def merge_junit(paths, output, wall_seconds):
    merged = ET.Element("testsuites", time=str(wall_seconds))
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Missing JUnit: {path}")
        root = ET.parse(path).getroot()
        merged.extend([root] if root.tag == "testsuite" else root.findall("testsuite"))
    ET.ElementTree(merged).write(output, encoding="utf-8", xml_declaration=True)


def worker_environment(directory, index, port_base, port_width):
    environment = os.environ.copy()
    for name in ("PYTEST_ADDOPTS", "CI_REPORT_DIR", "CI_TEST_LANE", "HYPOTHESIS_PROFILE"):
        environment.pop(name, None)
    return phase_environment(environment, directory, index, port_base, port_width)


def phase_environment(base, directory, index, port_base, port_width):
    """Give one pytest process a private temporary directory and TCP port range."""
    start = port_base + index * port_width
    end = start + port_width - 1
    if start < 1024 or port_width < 1 or end > 65535 or start <= 17871 <= end:
        raise ValueError("Invalid worker port range")
    directory.mkdir(parents=True, exist_ok=True)
    environment = dict(base)
    for name in ("GITHUB_STEP_SUMMARY", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    environment.update(TEMP=str(directory.resolve()), TMP=str(directory.resolve()),
                       TMPDIR=str(directory.resolve()), PYTHONDONTWRITEBYTECODE="1",
                       LOCAL_BACKEND_PORT_START=str(start), LOCAL_BACKEND_PORT_END=str(end))
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return environment


def configure_worker_environment():
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    root = os.environ.get("LOCAL_BACKEND_WORK_ROOT")
    if worker and root:
        directory = Path(root) / worker
        environment = worker_environment(directory / "temp", int(worker.removeprefix("gw")),
                                         int(os.environ["LOCAL_BACKEND_PORT_BASE"]),
                                         int(os.environ["LOCAL_BACKEND_PORT_WIDTH"]))
        environment["CI_REPORT_DIR"] = str(directory)
        os.environ.update(environment)
        tempfile.tempdir = None


def port_factory():
    """Allocate explicit test ports from this process's assigned range."""
    ports = iter(range(int(os.environ["LOCAL_BACKEND_PORT_START"]), int(os.environ["LOCAL_BACKEND_PORT_END"]) + 1))

    def allocate():
        for port in ports:
            try:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
        raise RuntimeError("Worker test port range exhausted")
    return allocate


class PortFixtures:
    @pytest.fixture
    def unused_tcp_port_factory(self):
        return port_factory()

    @pytest.fixture
    def unused_tcp_port(self, unused_tcp_port_factory):
        return unused_tcp_port_factory()


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    config.pluginmanager.register(PortFixtures(), "local-backend-port-fixtures")


def validate_execution(expected, reports):
    selected = [node for report in reports for node in report["selected"]]
    if collections.Counter(selected) != collections.Counter(expected):
        raise ValueError("Worker selection differs from the collected selection")
    for report in reports:
        if not report.get("complete") or report.get("collection_errors"):
            raise ValueError("Worker evidence is incomplete")
        executed = {phase["nodeid"] for phase in report["reports"]
                    if phase["when"] == "call" or phase["outcome"] in ("failed", "skipped")}
        if executed != set(report["selected"]):
            raise ValueError("Selected nodes were not all executed")


def build_command(directory, args, *, serial):
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "xdist",
            "-p", "tools.test_backend_parallel", "-p", "tools.ci_pytest",
            "-n", "0" if serial else str(args.workers), f"--dist={args.dist}", "--maxschedchunk=1", "--max-worker-restart=0",
            f"--basetemp={directory / 'basetemp'}", f"--junitxml={directory / 'junit.xml'}",
            "-o", "tmp_path_retention_policy=failed", "-o", "faulthandler_timeout=300",
            "-m", EXPRESSION + (" and serial" if serial else " and not serial"), *EXCLUSIONS]


def run_phase(command, root, directory, environment):
    with (directory / "pytest.log").open("wb") as stream:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        try:
            code = process.wait()
        except BaseException:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            process.wait()
            raise
    if code not in (0, 1, 5):
        raise ValueError(f"Pytest did not complete: {directory} (exit {code})")
    return code


PHASE_IDENTITY = ("schema", "source_commit", "workflow_run_id", "run_attempt", "job_id", "lane", "platform", "python",
                  "python_implementation", "pytest", "profile", "selection_profile", "collected", "deselected",
                  "collect_only", "partial", "selection")


def lane_phase_command(command, directory, phase, workers):
    """Direct one CI lane command's evidence to a phase directory and add its scheduler."""
    result = list(command)
    # Attached values keep existing phase directories out of pytest's rootdir inference.
    for option, value in (("--ci-output-dir", directory), ("--junitxml", directory / "junit.xml")):
        position = result.index(option)
        result[position:position + 2] = [f"{option}={value}"]
    position = result.index(f"--ci-output-dir={directory}")
    options = ["-p", "xdist", "-p", "tools.test_backend_parallel", "--ci-phase", phase,
               "-n", str(workers) if phase == "parallel" else "0", "--dist=load", "--maxschedchunk=1",
               "--max-worker-restart=0", f"--basetemp={directory / 'bt'}"]
    return result[:position] + options + result[position:]


def merge_lane_reports(reports, workers):
    """Combine the parallel and serial phase reports into one lane report."""
    if [report.get("phase", {}).get("name") for report in reports] not in (["parallel"], ["parallel", "serial"]):
        raise ValueError("Lane evidence requires a parallel phase optionally followed by a serial phase")
    first = reports[0]
    if any(report[key] != first[key] for report in reports[1:] for key in PHASE_IDENTITY):
        raise ValueError("Phase reports differ in identity or collection")
    selected = [node for report in reports for node in report["selected"]]
    if len(selected) != len(set(selected)):
        raise ValueError("A test was selected by more than one phase")
    serial = reports[1]["selected"] if len(reports) > 1 else []
    if (collections.Counter(first["phase"]["deferred"]) != collections.Counter(serial)
            or len(reports) > 1 and collections.Counter(reports[1]["phase"]["deferred"]) != collections.Counter(first["selected"])):
        raise ValueError("Phase selections do not partition the lane selection")
    codes = [0 if report["exit_code"] == 5 and not report["selected"] else report["exit_code"] for report in reports]
    merged = {key: value for key, value in first.items() if key != "phase"}
    merged.update(
        selected=selected, reports=[phase for report in reports for phase in report["reports"]],
        collection_errors=[error for report in reports for error in report["collection_errors"]],
        exit_code=next((code for code in codes if code), 0 if selected else first["exit_code"]),
        wall_seconds=round(sum(report["wall_seconds"] for report in reports), 6),
        complete=all(report["complete"] is True for report in reports),
        execution={"workers": workers, "phases": [
            {"name": report["phase"]["name"], "selected": len(report["selected"]),
             "wall_seconds": report["wall_seconds"], "exit_code": report["exit_code"]} for report in reports]})
    return merged


def run_lane(command, environment, output, *, workers, work_root, run, port_base=20000, port_width=512):
    """Run a CI lane on workers, then its serial tests in one process, and write one lane report."""
    from tools import ci_pytest

    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="l", dir=work_root)).resolve()
    reports = []
    for phase in ("parallel", "serial"):
        if reports and not reports[0]["phase"]["deferred"]:
            break
        directory = work / phase
        index = workers if phase == "serial" else workers + 1
        phase_env = phase_environment(environment, directory / "temp", index, port_base, port_width)
        phase_env.update(LOCAL_BACKEND_WORK_ROOT=str(directory), LOCAL_BACKEND_PORT_BASE=str(port_base),
                         LOCAL_BACKEND_PORT_WIDTH=str(port_width))
        code = run(lane_phase_command(command, directory, phase, workers), phase_env)
        if not (directory / "report.json").is_file():
            raise ValueError(f"The {phase} phase wrote no report (exit {code})")
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if report["exit_code"] != code or report.get("phase", {}).get("name") != phase:
            raise ValueError(f"The {phase} phase report differs from its process")
        reports.append(report)
    merged = merge_lane_reports(reports, workers)
    output.mkdir(parents=True, exist_ok=True)
    junits = [work / report["phase"]["name"] / "junit.xml" for report in reports]
    if all(path.is_file() for path in junits):
        merge_junit(junits, output / "junit.xml", merged["wall_seconds"])
    for journal in sorted(work.glob("*/progress*.jsonl")):
        shutil.copyfile(journal, output / f"{journal.stem}-{journal.parent.name}.jsonl")
    ci_pytest.write_report(output, merged)
    if not merged["complete"]:
        print("Worker evidence is incomplete; see the phase journals", file=sys.stderr, flush=True)
    return merged["exit_code"] or (0 if merged["complete"] else 1)


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--dist", choices=("load", "loadfile", "worksteal"), default="load", help="Choose test queues, whole files or work stealing")
    parser.add_argument("--work-dir", type=Path, required=True, help="Fresh directory for all logs and temporary files")
    parser.add_argument("--baseline-junit", type=Path, help="Compare per-node JUnit outcomes and retain differences")
    parser.add_argument("--port-base", type=int, default=20000)
    parser.add_argument("--port-width", type=int, default=512)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    try:
        import xdist  # noqa: F401
    except ImportError:
        parser.error("Install requirements-dev.txt in the test environment first")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    environment = worker_environment(work / "collection-temp", args.workers + 1, args.port_base, args.port_width)
    guard = subprocess.run([sys.executable, "-c", "import app; print(app.__file__)"], cwd=root,
                           env=environment, capture_output=True, text=True, check=True)
    if not Path(guard.stdout.strip()).resolve().is_relative_to(root.resolve()):
        raise ValueError("Import guard resolved outside the checkout")
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
               "-p", "tools.ci_pytest", f"--ci-output-dir={work / 'collection'}",
               f"--basetemp={work / 'collection-basetemp'}", "-m", EXPRESSION, *EXCLUSIONS]
    with (work / "collection.log").open("wb") as stream:
        code = subprocess.run(command, cwd=root, env=environment, stdout=stream, stderr=subprocess.STDOUT).returncode
    if code:
        raise ValueError(f"Collection failed: {work / 'collection.log'} (exit {code})")
    inventory = json.loads((work / "collection/report.json").read_text(encoding="utf-8"))
    results = []
    for serial in (False, True):
        directory = work / ("serial" if serial else "parallel")
        environment = worker_environment(directory / "temp", args.workers if serial else args.workers + 1,
                                         args.port_base, args.port_width)
        environment.update(CI_REPORT_DIR=str(directory), LOCAL_BACKEND_WORK_ROOT=str(directory),
                           LOCAL_BACKEND_PORT_BASE=str(args.port_base), LOCAL_BACKEND_PORT_WIDTH=str(args.port_width))
        print(f"Starting {'serial pass' if serial else str(args.workers) + ' workers (' + args.dist + ')'}", flush=True)
        code = run_phase(build_command(directory, args, serial=serial), root, directory, environment)
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if report["exit_code"] != code:
            raise ValueError("Pytest exit status differs from its report")
        results.append((directory, report, code))
        print(f"Completed {directory.name}: exit {code}, {report['wall_seconds']:.1f}s", flush=True)
    validate_execution(inventory["selected"], [report for _, report, _ in results])
    elapsed = time.monotonic() - started
    merge_junit([directory / "junit.xml" for directory, _, _ in results], work / "junit.xml", elapsed)
    outcomes = junit_outcomes(work / "junit.xml")
    baseline = junit_outcomes(args.baseline_junit) if args.baseline_junit else outcomes
    differences = [{"nodeid": node, "baseline": baseline.get(node), "parallel": outcomes.get(node)}
                   for node in sorted(baseline.keys() | outcomes.keys()) if baseline.get(node) != outcomes.get(node)]
    summary = dict(wall_seconds=elapsed, workers=args.workers, dist=args.dist, selected=len(inventory["selected"]),
                   outcomes=dict(collections.Counter(outcomes.values())), differences=differences,
                   missing=sorted(baseline.keys() - outcomes.keys()), unexpected=sorted(outcomes.keys() - baseline.keys()))
    (work / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key not in ("differences", "missing", "unexpected")}), flush=True)
    print(f"Outcome differences: {len(differences)}. Reports: {work}", flush=True)
    return 1 if differences or any(code == 1 for _, _, code in results) else 0


configure_worker_environment()

if __name__ == "__main__":
    raise SystemExit(main())
