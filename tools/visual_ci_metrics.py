"""Measure visual scheduling, runner use and phases without qualifying coverage.

Each directory contains run.json and the complete filter=all jobs.json history.
Capture preparation also appears separately in the verified execution receipt;
the hosted capture step includes that preparation and must not be read as pure
browser execution time.
Shared timing resolves retained rerun aliases through the provenance gate;
copied metadata does not count as another runner execution.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from tools import browser_ci_metrics as timing
except ModuleNotFoundError:
    import browser_ci_metrics as timing


LEGACY_DEPENDENCIES = {
    "visual-contracts": (),
    "visual-capture-dynamic": (),
    "visual-capture-remaining": (),
    "visual-capture-serial-control": ("visual-contracts",),
    "visual-capture": ("visual-contracts", "visual-capture-dynamic", "visual-capture-remaining",
                       "visual-capture-serial-control"),
}
DEPENDENCIES = {
    **{name: deps for name, deps in LEGACY_DEPENDENCIES.items()
       if name not in {"visual-capture-dynamic", "visual-capture"}},
    "visual-capture-dynamic-a": (), "visual-capture-dynamic-b": (),
    "visual-capture": ("visual-contracts", "visual-capture-dynamic-a", "visual-capture-dynamic-b",
                       "visual-capture-remaining", "visual-capture-serial-control"),
}
EXECUTION = {
    "Test extension surface interactions", "Test visual capture contracts", "Test visual output safety",
    "Test restricted browser reporting", "Probe the actual pinned Playwright reporter",
    "Test existing browser diagnostic redaction", "Test Python session diagnostics explicitly",
    "Test visual inventory and output safety", "Probe actual Playwright visual output destinations",
    "Capture visual stage group", "Capture serial visual control", "Record color qualification",
    "Capture the required visual stage group",
}
ASSEMBLY = {
    "Seal visual producer evidence", "Retain immutable visual producer evidence",
    "Retain restricted visual diagnostics", "Retrieve immutable visual producer evidence",
    "Validate and assemble visual evidence", "Retain verified visual assembly provenance", "Upload visual states",
    "Seal approved visual producer evidence", "Retain immutable complete visual inputs",
    "Retrieve immutable complete visual inputs",
    "Validate and assemble complete visual coverage", "Bind the final color report and approved captures",
}


def summarize(run, document):
    names = {row.get("name") for row in document.get("jobs", [])}
    if "visual-capture-dynamic" in names and names & {"visual-capture-dynamic-a", "visual-capture-dynamic-b"}:
        raise timing.TimingError("mixed visual job topology")
    dependencies = LEGACY_DEPENDENCIES if "visual-capture-dynamic" in names else DEPENDENCIES
    result = timing.summarize(run, document, dependencies=dependencies,
        execution_names={name: EXECUTION for name in dependencies}, assembly_names=ASSEMBLY,
        optional_dependencies={"visual-capture-serial-control"}, schema="visual-ci-timing/v1",
        measurement_scope="visual jobs; capture-step duration includes internal preparation, reported separately in producer phase timings")
    result["qualification_claim"] = False
    return result


def observations(summaries):
    return timing.observations(summaries, LEGACY_DEPENDENCIES | DEPENDENCIES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        runs = [summarize(json.loads((directory/"run.json").read_text(encoding="utf-8-sig")),
                          json.loads((directory/"jobs.json").read_text(encoding="utf-8-sig")))
                for directory in args.directories]
        encoded = json.dumps(dict(runs=runs, observations=observations(runs), qualification_claim=False),
                             indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("visual_ci_timing_invalid_evidence")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
