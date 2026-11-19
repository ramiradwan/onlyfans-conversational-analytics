"""Opt-in pytest selection and execution evidence for local and hosted CI runs."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

# Subtests became part of pytest itself in pytest 9.
SubtestReport = getattr(pytest, "SubtestReport", None)


def pytest_addoption(parser):
    group = parser.getgroup("backend-ci")
    group.addoption("--ci-lane", default=None)
    group.addoption("--ci-shard", type=int, default=None)
    group.addoption("--ci-windows-shard", type=int, choices=(1, 2, 3), default=None)
    group.addoption("--ci-windows-manifest", default=None)
    group.addoption("--ci-profile", default=None)
    group.addoption("--ci-output-dir", default=os.environ.get("CI_REPORT_DIR"))
    group.addoption("--ci-manifest", default=None)
    group.addoption("--ci-validate", action="store_true", default=False)
    group.addoption("--ci-phase", choices=("parallel", "serial"), default=None,
                    help="Run only selected tests without (parallel) or with (serial) the serial marker")


def is_worker(config):
    return hasattr(config, "workerinput")


def is_distributed(config):
    return config.pluginmanager.hasplugin("dsession")


def pytest_configure(config):
    if config.getoption("ci_windows_shard") is not None:
        from tools.ci_windows_shards import LEGACY_WINDOWS_EXPRESSION
        if config.getoption("ci_lane") or config.getoption("ci_shard") or config.getoption("ci_profile"):
            raise pytest.UsageError("--ci-windows-shard cannot use --ci-lane, --ci-shard or --ci-profile")
        if config.option.markexpr and config.option.markexpr != LEGACY_WINDOWS_EXPRESSION:
            raise pytest.UsageError("Full Windows shards use the frozen legacy marker expression; narrow locally with a node ID or -k")
        config.option.markexpr = LEGACY_WINDOWS_EXPRESSION
    config._ci_evidence = Evidence(config)


class Evidence:
    def __init__(self, config):
        self.config = config
        self.start = time.monotonic()
        self.collected = []
        self.selected = []
        self.deselected = []
        self.reports = []
        self.collection_errors = []
        self.deferred = []
        self.worker_collections = {}
        self.worker_failures = []
        self.progress_stream = None
        self.progress_sequence = 0
        self.watchdog = None

    def lane(self):
        windows_shard = self.config.getoption("ci_windows_shard")
        if windows_shard is not None:
            return f"windows-full-regression-{windows_shard}"
        lane = self.config.getoption("ci_lane") or "legacy"
        shard = self.config.getoption("ci_shard")
        if lane == "integration" and shard is not None:
            lane = f"analytics-integration-{shard}"
        return os.environ.get("CI_TEST_LANE") or lane

    def output_directory(self):
        return Path(self.config.getoption("ci_output_dir") or Path("artifacts/ci-tests") / self.lane())

    def start_progress(self):
        try:
            output = self.output_directory()
            output.mkdir(parents=True, exist_ok=True)
            name = (f"progress-{self.config.workerinput['workerid']}.jsonl" if is_worker(self.config)
                    else "progress.jsonl")
            self.progress_stream = (output / name).open("w", encoding="utf-8")
            host = {}
            if not is_worker(self.config):
                host = {"logical_processors": os.cpu_count(), "runner_image": os.environ.get("ImageOS", ""),
                        "runner_image_version": os.environ.get("ImageVersion", ""), "cpu_model": platform.processor()}
                if sys.platform.startswith("linux"):
                    try:
                        host["cpu_model"] = next((line.partition(":")[2].strip()
                            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
                            if line.startswith("model name")), host["cpu_model"])
                    except OSError:
                        pass
            self.progress("session_start", **host, source_commit=os.environ.get("PRODUCT_SHA", ""),
                          workflow_run_id=os.environ.get("GITHUB_RUN_ID", ""),
                          run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                          job_id=os.environ.get("GITHUB_JOB", "local"), lane=self.lane(), pid=os.getpid(),
                          started_at=round(time.time() - (time.monotonic() - self.start), 3))
        except OSError as error:
            print(f"Could not open optional CI progress journal: {error}", file=sys.stderr)

    def progress(self, event, **fields):
        if self.progress_stream is None:
            return
        self.progress_sequence += 1
        entry = dict(sequence=self.progress_sequence, elapsed_seconds=round(time.monotonic() - self.start, 6),
                     event=event, **fields)
        try:
            self.progress_stream.write(json.dumps(entry) + "\n")
            self.progress_stream.flush()
        except OSError as error:
            self.close_progress()
            print(f"Could not write optional CI progress journal: {error}", file=sys.stderr)

    def start_watchdog(self):
        seconds = os.environ.get("CI_PYTEST_WATCHDOG_SECONDS")
        if not seconds or is_worker(self.config) or self.config.option.collectonly:
            return
        from tools import ci_pytest_watchdog
        try:
            self.watchdog = ci_pytest_watchdog.start(
                self.output_directory(), seconds, distributed=is_distributed(self.config))
        except (OSError, ValueError) as error:
            print(f"[ci-watchdog] could not start: {error}", file=sys.stderr, flush=True)

    def stop_watchdog(self):
        if self.watchdog is not None:
            from tools import ci_pytest_watchdog
            process, self.watchdog = self.watchdog, None
            ci_pytest_watchdog.stop(process)

    def close_progress(self):
        stream, self.progress_stream = self.progress_stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def selection_is_partial(self):
        # The frozen exhaustive Windows predicate is the canonical legacy
        # selection, not an arbitrary narrower -m filter. Its raw inventory
        # may be consumed only after node-level selection is independently
        # checked against the exact predicate by ci_windows_shards.
        marker = self.config.option.markexpr or ""
        from tools.ci_windows_shards import LEGACY_WINDOWS_EXPRESSION
        return bool(self.config.option.keyword or (marker and marker != LEGACY_WINDOWS_EXPRESSION))

    def collection_view(self):
        return {"collected": self.collected, "selected": self.selected,
                "deselected": list(dict.fromkeys(self.deselected)), "deferred": self.deferred}

    def adopt_worker_collection(self):
        """Use the identical collection every worker reported; otherwise the evidence is incomplete."""
        views = list(self.worker_collections.values())
        expected = self.config.getoption("numprocesses", None)
        if (not views or self.worker_failures or any(view != views[0] for view in views)
                or isinstance(expected, int) and len(views) != expected):
            return False
        self.collected, self.selected, self.deselected, self.deferred = (
            views[0][key] for key in ("collected", "selected", "deselected", "deferred"))
        return True

    def payload(self, exit_code):
        complete = self.adopt_worker_collection() if is_distributed(self.config) else True
        phase = self.config.getoption("ci_phase")
        return {
            "schema": "ci-test-report/v1",
            "source_commit": os.environ.get("PRODUCT_SHA", ""),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "job_id": os.environ.get("GITHUB_JOB", "local"),
            "lane": self.lane(),
            "platform": platform.system(),
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "pytest": pytest.__version__,
            "profile": os.environ.get("HYPOTHESIS_PROFILE", ""),
            "selection_profile": self.config.getoption("ci_profile") or "",
            "collected": self.collected,
            "selected": self.selected,
            "deselected": list(dict.fromkeys(self.deselected)),
            "reports": self.reports,
            "collection_errors": self.collection_errors,
            "exit_code": int(exit_code),
            "wall_seconds": round(time.monotonic() - self.start, 6),
            "collect_only": bool(self.config.option.collectonly),
            "partial": self.selection_is_partial(),
            "selection": {"keyword": self.config.option.keyword or "", "marker": self.config.option.markexpr or "", "targets": list(self.config.args)},
            "complete": complete,
            **({"phase": {"name": phase, "deferred": self.deferred}} if phase else {}),
        }


def _describe(item):
    return {
        "nodeid": item.nodeid,
        "path": item.nodeid.split("::", 1)[0],
        "markers": sorted({mark.name for mark in item.iter_markers()}),
    }


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    evidence = config._ci_evidence
    evidence.collected = [_describe(item) for item in items]
    lane = config.getoption("ci_lane")
    windows_shard = config.getoption("ci_windows_shard")
    if windows_shard is not None:
        from tools.ci_windows_shards import WindowsShardError, load_manifest, selected_nodeids
        # Full default collection fails closed; positional targeting/--ignore is
        # useful locally, and hosted parity separately refuses narrowed evidence.
        full_collection = (getattr(config.args_source, "name", "ARGS") != "ARGS"
                           and not config.option.ignore and not config.option.ignore_glob)
        try:
            manifest = load_manifest(config.rootpath, config.getoption("ci_windows_manifest"))
            wanted = selected_nodeids(evidence.collected, manifest, windows_shard,
                                      complete=config.getoption("ci_validate") or full_collection)
        except (WindowsShardError, OSError, json.JSONDecodeError) as error:
            raise pytest.UsageError(str(error)) from error
        deselected = [item for item in items if item.nodeid not in wanted]
        items[:] = [item for item in items if item.nodeid in wanted]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
    elif lane:
        from tools.ci_selection import SelectionError, load_manifest, select_items

        try:
            manifest = load_manifest(config.rootpath, config.getoption("ci_manifest"))
            selected, deselected, inventory = select_items(
                items, manifest, lane,
                shard=config.getoption("ci_shard"), profile=config.getoption("ci_profile"),
            )
            if config.getoption("ci_validate"):
                from tools.ci_selection import validate_inventory
                validate_inventory(inventory, manifest, complete=True)
        except SelectionError as error:
            raise pytest.UsageError(str(error)) from error
        evidence.collected = [dict(raw, **metadata) for raw, metadata in zip(evidence.collected, inventory)]
        items[:] = selected
        if deselected:
            config.hook.pytest_deselected(items=deselected)
    yield
    phase = config.getoption("ci_phase")
    if phase:
        # The other phase owns these tests; they remain selected for the lane.
        serial = phase == "serial"
        evidence.deferred = [item.nodeid for item in items if (item.get_closest_marker("serial") is not None) != serial]
        items[:] = [item for item in items if (item.get_closest_marker("serial") is not None) == serial]
    evidence.selected = [item.nodeid for item in items]
    if is_worker(config):
        config.workeroutput["ci_collection"] = evidence.collection_view()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    evidence = node.config._ci_evidence
    view = getattr(node, "workeroutput", {}).get("ci_collection")
    if error is not None or view is None:
        evidence.worker_failures.append(node.gateway.id)
    else:
        evidence.worker_collections[node.gateway.id] = view


def pytest_deselected(items):
    if items:
        items[0].config._ci_evidence.deselected.extend(item.nodeid for item in items)


def _context_kwargs(report, distributed):
    kwargs = dict(report.context.kwargs)
    if distributed:
        # Deserializing a worker's subtest context applies saferepr a second time.
        for key, value in kwargs.items():
            try:
                inner = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(inner, str):
                kwargs[key] = inner
    return kwargs


def _command_escape(value, *, property_value=False):
    value = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def annotate_failure(evidence, report):
    """Emit bounded, payload-free diagnostics from the controlling pytest process."""
    config = evidence.config
    if is_worker(config) or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    workspace = os.environ.get("GITHUB_WORKSPACE")
    temp = os.environ.get("RUNNER_TEMP")
    if not workspace or not temp:
        return
    try:
        if Path(workspace).resolve() != config.rootpath.resolve():
            return
    except (OSError, RuntimeError, ValueError):
        return
    # All controllers in a hosted job run sequentially; RUNNER_TEMP is job-local.
    # Exclusive creation also bounds the budget if controllers overlap.
    identity = hashlib.sha256(json.dumps([os.environ.get(k, "") for k in
        ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")]).encode()).hexdigest()
    budget = Path(temp) / ("ofca-failure-annotations-" + identity)
    crash = getattr(report.longrepr, "reprcrash", None)
    raw_message = getattr(crash, "message", "")
    if not isinstance(raw_message, str):
        raw_message = ""
    match = re.match(r"([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception|Failure))\b", raw_message)
    classification = match.group(1) if match else "test failure"
    # Only a fixed vocabulary is displayed; never include exception messages.
    if classification not in {"AssertionError", "IndexError", "KeyError", "TypeError", "ValueError",
                              "RuntimeError", "TimeoutError", "OSError", "FileNotFoundError", "AttributeError"}:
        classification = "test failure"
    location = getattr(report, "location", None)
    path = None
    if location:
        try:
            candidate = (config.rootpath / location[0]).resolve()
            if candidate.is_relative_to(config.rootpath.resolve()) and candidate.is_file():
                path = candidate.relative_to(config.rootpath.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            pass  # An unavailable source location must not prevent reporting.
    nodeid = report.nodeid
    # An external source location must not expose an absolute machine path.
    if path is None:
        nodeid = nodeid.partition("::")[2] or "test-id:" + hashlib.sha256(nodeid.encode()).hexdigest()[:12]
    # Parameter values can contain arbitrary fixture data. Keep the test name,
    # and identify parametrizations by digest rather than exposing their values.
    if "[" in nodeid:
        base, parameters = nodeid.split("[", 1)
        nodeid = base + "[id:" + hashlib.sha256(parameters.encode()).hexdigest()[:12] + "]"
    signature = [report.when, report.nodeid]
    if report.when != "call" and crash is not None:
        signature = [report.when, str(crash.path), crash.lineno, classification]
    digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    try:
        budget.mkdir(exist_ok=True)
        # Claim the signature, then one of eight slots with exclusive creates.
        # No persistent lock can block a later invocation after a worker dies.
        signature_path = budget / ("signature-" + digest)
        try:
            signature_path.touch(exist_ok=False)
        except FileExistsError:
            return
        for slot in range(8):
            try:
                (budget / f"slot-{slot}").touch(exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            signature_path.unlink(missing_ok=True)
            try:
                (budget / "overflow").touch(exist_ok=False)
            except FileExistsError:
                return
            print("\n::notice::Additional test failures are in the full logs and execution report.",
                  file=sys.__stdout__ or sys.stdout, flush=True)
            return
    except OSError:
        return  # Diagnostics must never change the test outcome.
    profile = config.getoption("ci_profile") or os.environ.get("HYPOTHESIS_PROFILE", "") or "default"
    message = f"{nodeid} | {report.when} | {evidence.lane()} | {profile} | {classification}"
    properties = ""
    if path is not None:
        properties = " file=" + _command_escape(path, property_value=True)
        if isinstance(location[1], int) and location[1] >= 0:
            properties += f",line={location[1] + 1}"
    print(f"\n::error{properties}::{_command_escape(message)}", file=sys.__stdout__ or sys.stdout, flush=True)


class ReportCollector:
    def __init__(self, evidence):
        self.evidence = evidence
        self.subtest_counts = {}

    def pytest_runtest_logstart(self, nodeid, location):
        self.evidence.progress("test_start", nodeid=nodeid)
        # File progress is always retained. Console breadcrumbs are enabled in
        # hosted Windows jobs, where a hard timeout can prevent artifact upload.
        if os.environ.get("CI_PYTEST_LIVE_PROGRESS") == "1":
            print(f"[ci-progress] start {nodeid}", file=sys.__stdout__ or sys.stdout, flush=True)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.evidence.progress("test_finish", nodeid=nodeid)

    def _phase(self, item, when):
        self.evidence.progress("phase_start", nodeid=item.nodeid, when=when)
        try:
            yield
        finally:
            self.evidence.progress("phase_finish", nodeid=item.nodeid, when=when)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        yield from self._phase(item, "setup")

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        yield from self._phase(item, "call")

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item):
        yield from self._phase(item, "teardown")

    def pytest_runtest_logreport(self, report):
        if report.failed:
            annotate_failure(self.evidence, report)
        skip_reason = None
        if report.skipped:
            skip_reason = getattr(report, "wasxfail", None)
            if not skip_reason:
                skip_reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        phase = {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
            "wasxfail": getattr(report, "wasxfail", None),
            "skip_reason": skip_reason,
            "detail": str(report.longrepr) if report.failed or report.skipped else "",
        }
        if SubtestReport is not None and isinstance(report, SubtestReport):
            index = self.subtest_counts.get(report.nodeid, 0) + 1
            self.subtest_counts[report.nodeid] = index
            phase["subtest"] = {
                "index": index,
                "msg": report.context.msg,
                # Pytest already converts each context value to a safe repr.
                "kwargs": _context_kwargs(report, is_distributed(self.evidence.config)),
            }
        self.evidence.reports.append(phase)
        self.evidence.progress("phase_report", **{key: value for key, value in phase.items() if key != "detail"})

    def pytest_collectreport(self, report):
        if report.failed:
            self.evidence.collection_errors.append({"nodeid": report.nodeid, "detail": str(report.longrepr)})


def pytest_sessionstart(session):
    session.config._ci_evidence.start_progress()
    session.config._ci_evidence.start_watchdog()
    session.config.pluginmanager.register(ReportCollector(session.config._ci_evidence), "ci-phase-collector")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection(session):
    evidence = session.config._ci_evidence
    evidence.progress("collection_start")
    try:
        yield
    finally:
        evidence.progress("collection_finish", collected=len(evidence.collected), selected=len(evidence.selected))


def pytest_unconfigure(config):
    evidence = getattr(config, "_ci_evidence", None)
    if evidence is not None:
        evidence.close_progress()
        evidence.stop_watchdog()


def summary(payload):
    outcomes = {}
    priority = {"passed": 0, "skipped": 1, "failed": 2}
    for report in payload["reports"]:
        previous = outcomes.get(report["nodeid"])
        if (report["when"] == "call" or report["outcome"] in {"failed", "skipped"}) and (
            previous is None or priority[report["outcome"]] > priority[previous]
        ):
            outcomes[report["nodeid"]] = report["outcome"]
    counts = {name: sum(value == name for value in outcomes.values()) for name in ("passed", "failed", "skipped")}
    lines = [f"# Backend: {payload['lane']}", "", f"{len(payload['selected'])} selected · {len(payload['deselected'])} deselected · "
             f"{counts['passed']} passed · {counts['failed']} failed · {counts['skipped']} skipped · {payload['wall_seconds']:.1f}s", ""]
    if payload["lane"].startswith("analytics-integration-") and payload["wall_seconds"] > 600:
        lines.extend(["**Timing warning:** this shard exceeded the 10-minute maintenance budget. Review its complete timings before rebalancing.", ""])
    for failure in payload["collection_errors"]:
        lines.extend([f"Collection failed: `{failure['nodeid']}`", "", "```text", failure["detail"][-6000:], "```", ""])
    for report in payload["reports"]:
        if report["outcome"] != "failed":
            continue
        lines.extend([f"**{report['when']} failure:** `{report['nodeid']}`", "", "```text", report["detail"][-6000:], "```", ""])
        if "subtest" in report:
            lines.extend(["Subtest context: `" + json.dumps(report["subtest"], sort_keys=True) + "`", ""])
        if (payload["selection_profile"] or payload["profile"]) and Path(__file__).with_name("test_backend.py").is_file():
            command = f'python tools/test_backend.py stateful --profile {payload["selection_profile"] or payload["profile"]} -- "{report["nodeid"]}" -vv'
        else:
            command = f'python -m pytest "{report["nodeid"]}" -vv --override-ini=addopts='
            if payload["profile"]:
                if payload["platform"] == "Windows":
                    command = f'$env:HYPOTHESIS_PROFILE="{payload["profile"]}"; ' + command
                else:
                    command = f'HYPOTHESIS_PROFILE="{payload["profile"]}" ' + command
        lines.extend([f"Reproduce on {payload['platform']} / Python {payload['python']}:", "", "```text", command, "```", ""])
    lines.extend(["<details><summary>Slowest test phases</summary>", "", "| Test | Phase | Seconds |", "| --- | --- | ---: |"])
    for report in sorted(payload["reports"], key=lambda value: value["duration"], reverse=True)[:10]:
        lines.append(f"| `{report['nodeid'].replace('|', '&#124;')}` | {report['when']} | {report['duration']:.3f} |")
    lines.extend(["", "</details>", ""])
    return "\n".join(lines)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    evidence = session.config._ci_evidence
    evidence.progress("session_finish", exit_code=int(exitstatus))
    evidence.close_progress()
    evidence.stop_watchdog()
    if is_worker(session.config):
        return  # The controller records the merged worker evidence.
    output = evidence.output_directory()
    write_report(output, evidence.payload(exitstatus), getattr(session.config.option, "xmlpath", None))


def write_report(output, payload, junit=None):
    """Write the report, durations, JUnit copy and summary for one lane execution."""
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "report.json.tmp"
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "report.json")
    slowest = sorted(payload["reports"], key=lambda value: value["duration"], reverse=True)[:50]
    (output / "durations.txt").write_text("\n".join(
        f"{item['duration']:.6f}s {item['when']} {item['nodeid']}" for item in slowest
    ) + "\n", encoding="utf-8")
    if junit and Path(junit).is_file() and Path(junit).resolve() != (output / "junit.xml").resolve():
        shutil.copyfile(junit, output / "junit.xml")
    try:
        rendered = summary(payload)
        (output / "summary.md").write_text(rendered, encoding="utf-8")
        github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if github_summary:
            with Path(github_summary).open("a", encoding="utf-8") as stream:
                stream.write(rendered)
    except OSError as error:
        print(f"Could not write optional CI summary: {error}", file=sys.stderr)
