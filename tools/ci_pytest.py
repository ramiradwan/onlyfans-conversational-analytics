"""Opt-in pytest selection and execution evidence for local and hosted CI runs."""
from __future__ import annotations

import json
import os
import platform
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
    group.addoption("--ci-windows-shard", type=int, choices=(1, 2), default=None)
    group.addoption("--ci-windows-manifest", default=None)
    group.addoption("--ci-profile", default=None)
    group.addoption("--ci-output-dir", default=os.environ.get("CI_REPORT_DIR"))
    group.addoption("--ci-manifest", default=None)
    group.addoption("--ci-validate", action="store_true", default=False)


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
        self.progress_stream = None
        self.progress_sequence = 0

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
            self.progress_stream = (output / "progress.jsonl").open("w", encoding="utf-8")
            self.progress("session_start", source_commit=os.environ.get("PRODUCT_SHA", ""),
                          workflow_run_id=os.environ.get("GITHUB_RUN_ID", ""),
                          run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                          job_id=os.environ.get("GITHUB_JOB", "local"), lane=self.lane(), pid=os.getpid())
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

    def close_progress(self):
        stream, self.progress_stream = self.progress_stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def payload(self, exit_code):
        return {
            "schema": "ci-test-report/v1",
            "source_commit": os.environ.get("PRODUCT_SHA", ""),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "job_id": os.environ.get("GITHUB_JOB", "local"),
            "lane": self.lane(),
            "platform": platform.system(),
            "python": platform.python_version(),
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
            "partial": bool(self.config.option.keyword or self.config.option.markexpr),
            "selection": {"keyword": self.config.option.keyword or "", "marker": self.config.option.markexpr or "", "targets": list(self.config.args)},
            "complete": True,
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
    evidence.selected = [item.nodeid for item in items]


def pytest_deselected(items):
    if items:
        items[0].config._ci_evidence.deselected.extend(item.nodeid for item in items)


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
                "kwargs": dict(report.context.kwargs),
            }
        self.evidence.reports.append(phase)
        self.evidence.progress("phase_report", **{key: value for key, value in phase.items() if key != "detail"})

    def pytest_collectreport(self, report):
        if report.failed:
            self.evidence.collection_errors.append({"nodeid": report.nodeid, "detail": str(report.longrepr)})


def pytest_sessionstart(session):
    session.config._ci_evidence.start_progress()
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
    payload = evidence.payload(exitstatus)
    output = evidence.output_directory()
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "report.json.tmp"
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "report.json")
    slowest = sorted(payload["reports"], key=lambda value: value["duration"], reverse=True)[:50]
    (output / "durations.txt").write_text("\n".join(
        f"{item['duration']:.6f}s {item['when']} {item['nodeid']}" for item in slowest
    ) + "\n", encoding="utf-8")
    junit = getattr(session.config.option, "xmlpath", None)
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
