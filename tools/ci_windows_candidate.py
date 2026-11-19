"""Independent, non-release qualification of the experimental Windows topology."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import ci_gate as gate
from tools.ci_windows_shards import candidate_groups, LEGACY_WINDOWS_EXPRESSION, _unique_pairs


def validate_candidate(reports, plan, contracts, *, source, run, attempt):
    reference = reports["legacy-windows-reference"]
    rows = gate.inventory(reference)
    expected = candidate_groups(rows.values(), plan, contracts)
    lanes = {**{f"candidate-{name}": name for name in expected}, "legacy-windows-reference": None}
    if set(reports) != set(lanes):
        raise gate.GateError("Candidate report set is incomplete")
    retained = set(plan["retain_in_full"])
    for lane, group in lanes.items():
        report = reports[lane]
        job = ("windows-qualification" if group is None else "windows-full-shards" if group.startswith("full-")
               else "windows-platform-contract" if group.startswith("platform-") else "analytics-windows-contract")
        if (report.get("schema") != "ci-test-report/v1" or report.get("lane") != lane
                or report.get("source_commit") != source or str(report.get("workflow_run_id")) != str(run)
                or str(report.get("run_attempt")) != str(attempt) or report.get("job_id") != job
                or report.get("partial") is not False or report.get("selection") != {
                    "keyword": "", "marker": LEGACY_WINDOWS_EXPRESSION, "targets": ["tests"]}):
            raise gate.GateError(f"{lane}: wrong source, producer, attempt or selection")
        outcomes = gate.validate_report(report, gate.ReportSpec(job, "Windows", reference=group is None))
        if gate._raw_inventory_signature(gate.inventory(report)) != gate._raw_inventory_signature(rows):
            raise gate.GateError(f"{lane}: raw inventory differs")
        if group is None:
            gate._exact_selection(report, set().union(*expected.values()), lane)
            continue
        gate._exact_selection(report, expected[group], lane)
        if report.get("execution", {}).get("workers") != 4 or report.get("execution", {}).get("dist") != "load":
            raise gate.GateError(f"{lane}: changed worker configuration")
        if (report.get("python"), report.get("python_implementation"), report.get("pytest")) != (
                reference.get("python"), reference.get("python_implementation"), reference.get("pytest")):
            raise gate.GateError(f"{lane}: changed Python or pytest identity")
        if not group.startswith("full-"):
            for node, outcome in outcomes.items():
                if node not in retained and outcome != "executed":
                    raise gate.GateError(f"{node}: omitted full-regression node did not execute in focused job")
    return {"default_nodes": len(set().union(*expected.values())), "full_nodes": sum(len(nodes) for group, nodes in expected.items() if group.startswith("full-")),
            "focused_nodes": sum(len(nodes) for group, nodes in expected.items() if not group.startswith("full-")),
            "retained_overlap": len(plan["retain_in_full"])}


def load_candidate_reports(directory, *, source, run, attempt):
    """Accept candidate reports only from their exact immutable artifact."""
    reports = {}
    for path in directory.rglob("report.json"):
        row = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        lane = row.get("lane", "")
        if not (lane.startswith("candidate-") or lane == "legacy-windows-reference"):
            continue
        producer = "windows-qualification" if lane == "legacy-windows-reference" else lane
        artifact = f"ci-tests-{producer}-{source}-{run}-{attempt}"
        if path.relative_to(directory).parts != (artifact, lane, "report.json") or lane in reports:
            raise gate.GateError("Candidate artifact path or duplicate report differs")
        reports[lane] = row
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    try:
        reports = load_candidate_reports(args.reports, source=os.environ["PRODUCT_SHA"],
                                         run=os.environ["GITHUB_RUN_ID"], attempt=os.environ["GITHUB_RUN_ATTEMPT"])
        plan = json.loads((ROOT / "tools/windows-topology-candidate.json").read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        contracts = json.loads((ROOT / "ci/backend-test-shards.json").read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)["windows_contracts"]
        result = validate_candidate(reports, plan, contracts, source=os.environ["PRODUCT_SHA"], run=os.environ["GITHUB_RUN_ID"], attempt=os.environ["GITHUB_RUN_ATTEMPT"])
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Candidate qualification refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
