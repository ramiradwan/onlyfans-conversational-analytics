"""Fail-closed aggregate CI result and independent legacy-selection parity.

The two legacy reference reports use the original pytest marker expressions,
without the new classifier. A successful collection is not execution evidence.
Reports from retained dependencies on a failed-jobs rerun remain valid only when
the Actions API identifies that attempt as that job's newest execution.
"""
from __future__ import annotations

import argparse
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from tools.engineering_attestation import ContractError, GitHubApi, latest_ci_jobs, load_json_strict
except ModuleNotFoundError:  # Direct `python tools/ci_gate.py` invocation.
    from engineering_attestation import ContractError, GitHubApi, latest_ci_jobs, load_json_strict


class GateError(ValueError):
    """A mandatory result or its evidence could not be established."""


@dataclass(frozen=True)
class ReportSpec:
    job: str
    platform: str
    profile: str = ""
    selection_profile: str = ""
    selectors: tuple[str, ...] = ()
    reference: bool = False

    @property
    def logical_job(self) -> str:
        return "analytics-integration" if self.job.startswith("analytics-integration-") else self.job


# Frozen from the old explicit CI invocations. A classifier or manifest edit
# cannot silently remove one of these profiles from mandatory PR coverage.
SPECS = {
    "backend-fast": ReportSpec("backend-fast", "Linux"),
    **{f"analytics-integration-{n}": ReportSpec(f"analytics-integration-{n}", "Linux") for n in range(1, 5)},
    "windows-platform-contract": ReportSpec("windows-platform-contract", "Windows"),
    "analytics-windows-contract": ReportSpec("analytics-windows-contract", "Windows"),
    "analytics-scale-qualification": ReportSpec("analytics-scale-qualification", "Windows"),
    "windows-full-regression": ReportSpec("windows-full-regression", "Windows"),
    "legacy-linux-reference": ReportSpec("backend-fast", "Linux", reference=True),
    "legacy-windows-reference": ReportSpec("windows-platform-contract", "Windows", reference=True),
    "backend-fast-brain-general": ReportSpec("backend-fast", "Linux", "tier_a_general", "tier_a_general", ("tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral",)),
    "backend-fast-brain-deletion": ReportSpec("backend-fast", "Linux", "tier_a_deletion", "tier_a_deletion", ("tests/stateful/test_brain_ingestion.py::TestBrainIngestionDeletion",)),
    "backend-fast-agent-general": ReportSpec("backend-fast", "Linux", "agent_tier_a_general", "agent_tier_a_general", ("tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral",)),
    "backend-fast-agent-deletion": ReportSpec("backend-fast", "Linux", "agent_tier_a_deletion", "agent_tier_a_deletion", ("tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion",)),
    "backend-fast-analytics-determinism": ReportSpec("backend-fast", "Linux", "analytics_determinism_fast", "analytics_determinism_fast", ("tests/stateful/test_analytics_determinism.py::TestAnalyticsDeterminism",)),
    "backend-fast-analytics-convergence": ReportSpec("backend-fast", "Linux", "analytics_convergence_fast", "analytics_convergence_fast", ("tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence",)),
    "backend-fast-analytics-deletion": ReportSpec("backend-fast", "Linux", "analytics_deletion_fast", "analytics_deletion_fast", ("tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence",)),
    "backend-fast-analytics-falsifiers": ReportSpec("backend-fast", "Linux", "", "analytics_oracle_falsifiers", tuple(
        "tests/stateful/test_analytics_equivalence.py::" + name for name in (
            "test_analytics_convergence_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults",
            "test_shared_oracle_rejects_same_forged_topic_or_entity_graph_in_both_artifacts",
            "test_shared_oracle_rejects_same_stale_deleted_message_metric_in_both_artifacts",
            "test_active_publication_oracle_rejects_stale_deleted_material_with_current_witness",
            "test_deliberate_falsifier_failure_configures_hypothesis_shrink_phase",
        )
    )),
    "backend-fast-contract-integrity": ReportSpec("backend-fast", "Linux"),
    "windows-production-boot": ReportSpec("windows-platform-contract", "Windows"),
    "windows-persistence-general": ReportSpec("windows-platform-contract", "Windows", "tier_b_general", "tier_b_general", ("tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral",)),
    "windows-persistence-deletion": ReportSpec("windows-platform-contract", "Windows", "tier_b_deletion", "tier_b_deletion", ("tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion",)),
    "windows-persistence-smoke": ReportSpec("windows-platform-contract", "Windows", "windows_persistence_smoke", "windows_persistence_smoke", ("tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke",)),
}
ACTUAL_JOBS = {spec.job for spec in SPECS.values()} | {
    "web-build-and-test", "fixed-sqlcipher-wheel", "windows-browser-e2e"
}
NEEDED_JOBS = {spec.logical_job for spec in SPECS.values()} | {
    "web-build-and-test", "fixed-sqlcipher-wheel", "windows-browser-e2e"
}
LEGACY_EXCLUDED = {"slow", "stateful_tier_a", "stateful_tier_b", "stateful_agent_tier_a"}
SCALE_LANE = "analytics-scale-qualification"
# These two files retain their existing packaged-runtime qualification boundary.
# Every other slow case, including newly added modules, must execute on main and
# nightly. Do not freeze today's analytics file list into an accidental opt-out.
SEPARATELY_QUALIFIED_SLOW_FILES = (
    "tests/test_packaged_runtime.py", "tests/test_installation_key.py",
)
SUPPORTED_EVENTS = {"pull_request", "push", "schedule", "workflow_dispatch"}


def required_report_lanes(event: str) -> set[str]:
    if event not in SUPPORTED_EVENTS:
        raise GateError(f"unsupported CI event {event!r}")
    return set(SPECS) - ({SCALE_LANE} if event == "pull_request" else set())


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise GateError(f"{label} must be a positive integer")
    return int(value)


def _unique_strings(value: Any, label: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise GateError(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise GateError(f"{label} contains duplicate identities")
    return set(value)


def validate_needs(needs: Any, jobs: dict[str, dict[str, Any]], *, event: str = "push") -> None:
    required_report_lanes(event)
    if not isinstance(needs, dict) or set(needs) != NEEDED_JOBS:
        raise GateError("gate dependencies do not match the mandatory execution jobs")
    for name, result in needs.items():
        expected = "skipped" if name == SCALE_LANE and event == "pull_request" else "success"
        if not isinstance(result, dict) or result.get("result") != expected:
            raise GateError(f"mandatory dependency {name} did not have required result {expected!r}")
    for name in sorted(ACTUAL_JOBS):
        job = jobs.get(name, {})
        expected = "skipped" if name == SCALE_LANE and event == "pull_request" else "success"
        if job.get("status") != "completed" or job.get("conclusion") != expected:
            raise GateError(f"latest mandatory job {name} did not complete with required result {expected!r}")


def inventory(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = report.get("collected")
    if not isinstance(rows, list):
        raise GateError("report is missing collection inventory")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("nodeid"), str) or not row["nodeid"]:
            raise GateError("invalid collected test identity")
        if row["nodeid"] in result:
            raise GateError("duplicate collected test identity")
        _unique_strings(row.get("markers"), "markers")
        result[row["nodeid"]] = row
    return result


def validate_report(report: dict[str, Any], spec: ReportSpec) -> dict[str, str]:
    """Return each selected node's executed, xfailed or skipped outcome."""
    if (report.get("complete") is not True or type(report.get("exit_code")) is not int
            or report.get("exit_code") != 0
            or report.get("collection_errors") != []
            or report.get("collect_only") is not spec.reference):
        raise GateError(f"{report['lane']}: incomplete, failed, or wrong-mode pytest evidence")
    elapsed = report.get("wall_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise GateError(f"{report['lane']}: invalid pytest elapsed time")
    if (report.get("platform") != spec.platform or report.get("profile") != spec.profile
            or report.get("selection_profile", "") != spec.selection_profile
            or not str(report.get("python", "")).startswith("3.11.")):
        raise GateError(f"{report['lane']}: platform, Python, or profile mismatch")
    rows = inventory(report)
    selected = _unique_strings(report.get("selected"), "selected")
    deselected = _unique_strings(report.get("deselected"), "deselected")
    if not selected or selected & deselected or selected | deselected != set(rows):
        raise GateError(f"{report['lane']}: selection does not partition the collected tests")
    phases = report.get("reports")
    if not isinstance(phases, list):
        raise GateError("missing pytest phase outcomes")
    if spec.reference:
        if phases:
            raise GateError("collection reference unexpectedly claims executed tests")
        return {}
    by_node: dict[str, dict[str, Any]] = {node: {} for node in selected}
    subtest_counts: Counter[str] = Counter()
    for phase in phases:
        if not isinstance(phase, dict) or phase.get("nodeid") not in selected:
            raise GateError("execution outcome has no selected test identity")
        node, when, outcome = phase["nodeid"], phase.get("when"), phase.get("outcome")
        if when not in {"setup", "call", "teardown"} or outcome not in {"passed", "skipped"}:
            raise GateError(f"{node}: failed or invalid pytest phase")
        if outcome == "skipped" and (
            not isinstance(phase.get("skip_reason"), str) or not phase["skip_reason"].strip()
        ):
            raise GateError(f"{node}: skipped phase has no structured skip reason")
        if "subtest" in phase:
            child = phase["subtest"]
            if (not isinstance(child, dict) or set(child) != {"index", "msg", "kwargs"}
                    or type(child["index"]) is not int
                    or child["index"] != subtest_counts[node] + 1
                    or child["msg"] is not None and not isinstance(child["msg"], str)
                    or not isinstance(child["kwargs"], dict)
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in child["kwargs"].items())):
                raise GateError(f"{node}: invalid or duplicate subtest identity")
            if when != "call" or outcome != "passed":
                raise GateError(f"{node}: subtest did not pass its call")
            subtest_counts[node] += 1
            continue
        if when in by_node[node]:
            raise GateError(f"{node}: duplicate pytest phase")
        by_node[node][when] = phase
    outcomes = {}
    for node, phases_by_name in by_node.items():
        if not {"setup", "teardown"} <= set(phases_by_name):
            raise GateError(f"{node}: incomplete pytest execution")
        setup = phases_by_name["setup"]
        call = phases_by_name.get("call")
        if setup["outcome"] == "passed" and call is None:
            raise GateError(f"{node}: successful setup without a call outcome")
        if setup["outcome"] == "skipped" and call is not None:
            raise GateError(f"{node}: skipped setup with a call outcome")
        if subtest_counts[node] and (setup["outcome"] != "passed" or call is None
                                     or call["outcome"] != "passed"):
            raise GateError(f"{node}: subtests have no successful parent call")
        terminal = call if call is not None else setup
        if phases_by_name["teardown"]["outcome"] != "passed":
            raise GateError(f"{node}: teardown did not pass")
        outcomes[node] = ("executed" if terminal["outcome"] == "passed" else
                          "xfailed" if terminal.get("wasxfail") else "skipped")
    return outcomes


def load_reports(directory: Path, *, source_commit: str, run_id: int,
                 jobs: dict[str, dict[str, Any]], event: str = "push") -> dict[str, dict[str, Any]]:
    expected_lanes = required_report_lanes(event)
    chosen = {}
    identities = set()
    paths = sorted(directory.rglob("*.json"))
    if not paths:
        raise GateError("no pytest evidence artifacts were downloaded")
    for path in paths:
        try:
            report = load_json_strict(path.read_bytes(), label="pytest evidence")
        except (OSError, UnicodeError, ValueError, ContractError) as exc:
            raise GateError(f"malformed evidence file: {path.name}") from exc
        if not isinstance(report, dict) or report.get("schema") != "ci-test-report/v1":
            raise GateError(f"unrecognized pytest evidence: {path.name}")
        lane = report.get("lane")
        if lane not in expected_lanes:
            raise GateError(f"unexpected report lane {lane!r}")
        spec = SPECS[lane]
        attempt = _integer(report.get("run_attempt"), "report attempt")
        if (report.get("source_commit") != source_commit
                or _integer(report.get("workflow_run_id"), "report run") != run_id
                or report.get("job_id") != spec.logical_job):
            raise GateError(f"{lane}: report source, run, or job identity mismatch")
        expected_artifact = f"ci-tests-{spec.job}-{source_commit}-{run_id}-{attempt}"
        relative = path.relative_to(directory)
        if len(relative.parts) < 2 or relative.parts[0] != expected_artifact:
            raise GateError(f"{lane}: artifact and report job/attempt identity mismatch")
        if (lane, attempt) in identities:
            raise GateError(f"{lane}: duplicate report for job execution")
        identities.add((lane, attempt))
        latest_attempt = jobs[spec.job]["run_attempt"]
        if attempt > latest_attempt:
            raise GateError(f"{lane}: evidence is newer than the Actions job execution")
        if attempt == latest_attempt:
            validate_report(report, spec)
            chosen[lane] = report
    if set(chosen) != expected_lanes:
        raise GateError("missing evidence for latest job executions: " + ", ".join(sorted(expected_lanes - set(chosen))))
    return chosen


def _matches(node: str, selector: str) -> bool:
    return node == selector or node.startswith(selector + "::") or node.startswith(selector + "[")


def _exact_selection(report: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(report["selected"])
    if actual != expected:
        raise GateError(f"{label}: selection parity failed; missing={sorted(expected - actual)[:8]}, extra={sorted(actual - expected)[:8]}")


def _skip_reason(report: dict[str, Any], node: str) -> str | None:
    for phase in report["reports"]:
        if phase["nodeid"] == node and phase["outcome"] == "skipped" and not phase.get("wasxfail"):
            return phase.get("skip_reason")
    return None


def validate_coverage(reports: dict[str, dict[str, Any]], *, event: str = "push") -> dict[str, int]:
    if set(reports) != required_report_lanes(event):
        raise GateError("execution report set does not match the CI event")
    references = {
        "Linux": reports["legacy-linux-reference"],
        "Windows": reports["legacy-windows-reference"],
    }
    inventories = {system: inventory(report) for system, report in references.items()}
    legacy = {}
    for system, rows in inventories.items():
        expected = {node for node, row in rows.items()
                    if not set(row["markers"]) & LEGACY_EXCLUDED
                    and not (system == "Linux" and "windows_production" in row["markers"])}
        _exact_selection(references[system], expected, f"independent {system} legacy reference")
        legacy[system] = expected
    linux_lanes = ["backend-fast"] + [f"analytics-integration-{n}" for n in range(1, 5)]
    linux_owners = Counter(node for lane in linux_lanes for node in reports[lane]["selected"])
    if set(linux_owners) != legacy["Linux"] or any(count != 1 for count in linux_owners.values()):
        raise GateError("Linux default coverage must equal the independent legacy selection with exactly one owner")
    for lane in linux_lanes:
        rows = inventory(reports[lane])
        if set(rows) != set(inventories["Linux"]):
            raise GateError(f"{lane}: collection differs from the independent Linux reference")
    for lane, spec in SPECS.items():
        if spec.selectors:
            expected = {node for node in inventories[spec.platform]
                        if any(_matches(node, selector) for selector in spec.selectors)}
            if not expected or any(not any(_matches(node, selector) for node in expected) for selector in spec.selectors):
                raise GateError(f"{lane}: a mandatory explicit profile selector no longer collects")
            _exact_selection(reports[lane], expected, lane)
    contract_expected = {node for node, row in inventories["Linux"].items()
                         if _matches(node, "tests/test_contract_snapshot.py") and "contract_integrity" in row["markers"]}
    _exact_selection(reports["backend-fast-contract-integrity"], contract_expected, "contract integrity")
    boot_expected = {node for node, row in inventories["Windows"].items()
                     if "windows_production" in row["markers"] and not {"slow", "stateful_tier_b"} & set(row["markers"])}
    _exact_selection(reports["windows-production-boot"], boot_expected, "Windows production boot")
    _exact_selection(reports["windows-full-regression"], legacy["Windows"], "Windows shadow regression")

    windows_lanes = ["windows-platform-contract", "analytics-windows-contract", "windows-production-boot"]
    windows_union = set().union(*(set(reports[lane]["selected"]) for lane in windows_lanes))
    windows_rows = inventory(reports["windows-platform-contract"])
    if set(windows_rows) != set(inventories["Windows"]):
        raise GateError("Windows contract collection differs from independent Windows reference")
    required_windows = {node for node in legacy["Windows"]
                        if windows_rows[node].get("windows_compat") is True
                        or windows_rows[node].get("windows_contract")
                        or "windows_production" in windows_rows[node]["markers"]
                        or node not in inventories["Linux"]}
    for lane, contract in (("windows-platform-contract", "platform"), ("analytics-windows-contract", "analytics")):
        expected = {node for node in legacy["Windows"] if windows_rows[node].get("windows_contract") == contract}
        _exact_selection(reports[lane], expected, lane)
    if not required_windows <= windows_union:
        raise GateError("mandatory Windows execution is missing: " + ", ".join(sorted(required_windows - windows_union)[:8]))
    if not legacy["Windows"] <= set(linux_owners) | windows_union:
        raise GateError("Windows legacy tests are missing from the new required lanes")

    outcomes = {lane: validate_report(report, SPECS[lane]) for lane, report in reports.items()}
    shadow = outcomes["windows-full-regression"]
    for node in legacy["Linux"] | legacy["Windows"]:
        lanes = windows_lanes if node in required_windows else linux_lanes + windows_lanes
        observed = [outcomes[lane][node] for lane in lanes if node in outcomes[lane]]
        if not any(value in {"executed", "xfailed"} for value in observed):
            # A pre-existing skip is retained only with independent old-path
            # execution corroboration. Collection alone cannot authorize it.
            old_reason = _skip_reason(reports["windows-full-regression"], node)
            corroborated = any(_skip_reason(reports[lane], node) == old_reason
                               for lane in lanes if node in outcomes[lane])
            if not observed or shadow.get(node) != "skipped" or not old_reason or not corroborated:
                raise GateError(f"{node}: selected coverage became skipped or lost its required OS")
    for lane, spec in SPECS.items():
        if spec.selectors and any(value == "skipped" for value in outcomes[lane].values()):
            raise GateError(f"{lane}: a mandatory explicit profile test was skipped")
    scale_expected = set()
    if event != "pull_request":
        scale_expected = {node for node, row in inventories["Windows"].items()
                          if "slow" in row["markers"]
                          and not any(_matches(node, path) for path in SEPARATELY_QUALIFIED_SLOW_FILES)}
        if not scale_expected:
            raise GateError("analytics scale qualification no longer collects any independent slow cases")
        _exact_selection(reports[SCALE_LANE], scale_expected, "analytics scale qualification")
        if any(value == "skipped" for value in outcomes[SCALE_LANE].values()):
            raise GateError("analytics scale qualification skipped a mandatory case")
    return {"legacy_linux": len(legacy["Linux"]), "legacy_windows": len(legacy["Windows"]),
            "required_windows": len(required_windows), "scale": len(scale_expected), "reports": len(reports)}


def write_summary(path: str | None, *, jobs: dict[str, dict[str, Any]],
                  reports: dict[str, dict[str, Any]], message: str) -> None:
    """A missing optional summary destination never changes the gate result."""
    if not path:
        return
    lines = ["## Required CI", "", message, "", "| Job | Latest result | Test selections |", "| --- | --- | ---: |"]
    for name in sorted(ACTUAL_JOBS):
        job = jobs.get(name, {})
        url = job.get("html_url", "")
        label = f"[{name}]({url})" if isinstance(url, str) and url.startswith(
            "https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/"
        ) else name
        lane_reports = [report for lane, report in reports.items()
                        if SPECS[lane].job == name and not SPECS[lane].reference]
        selections = str(sum(len(report["selected"]) for report in lane_reports)) if lane_reports else "—"
        result = job.get("conclusion") or job.get("status") or "unavailable"
        lines.append(f"| {label} | {result} | {selections} |")
    lines.extend(["", "Test selections include explicit profile and platform repetitions. Collection-only references are excluded.", ""])
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
    except OSError as exc:
        print(f"Could not append optional Required CI summary: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--needs-json", required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    jobs: dict[str, dict[str, Any]] = {}
    reports: dict[str, dict[str, Any]] = {}
    try:
        source = os.environ.get("PRODUCT_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", source):
            raise GateError("PRODUCT_SHA must identify the tested source commit")
        run_id = _integer(os.environ.get("GITHUB_RUN_ID"), "workflow run")
        attempt = _integer(os.environ.get("GITHUB_RUN_ATTEMPT"), "workflow attempt")
        event = os.environ.get("GITHUB_EVENT_NAME", "")
        required_report_lanes(event)
        client = GitHubApi(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
        jobs = latest_ci_jobs(client, run_id=run_id, run_attempt=attempt, source_commit=source)
        validate_needs(load_json_strict(args.needs_json.encode(), label="gate dependencies"), jobs, event=event)
        reports = load_reports(args.reports_dir, source_commit=source, run_id=run_id, jobs=jobs, event=event)
        counts = validate_coverage(reports, event=event)
        message = "Required CI passed: " + ", ".join(f"{name}={value}" for name, value in counts.items())
        result = 0
    except (GateError, ContractError, ValueError, OSError) as exc:
        message = f"Required CI failed: {exc}"
        result = 1
    print(message)
    write_summary(os.environ.get("GITHUB_STEP_SUMMARY"), jobs=jobs, reports=reports, message=message)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
