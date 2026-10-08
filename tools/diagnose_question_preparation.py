"""Small real-process comparison of legacy and prepared question inputs.

This is a development diagnostic, not a Windows-profile qualification result.
It uses small synthetic input and fewer samples; never add its results to a closure.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

from tools import analytics_qualification as q
from tools.analytics_qualification_process import supervise


CASES = (
    ("legacy-populated-fresh", "populated", "fresh", False),
    ("prepared-populated-fresh", "populated", "fresh", True),
    ("prepared-empty-fresh", "empty", "fresh", True),
    ("prepared-populated-idle", "populated", "idle", True),
    ("prepared-populated-mutated", "populated", "mutated", True),
    ("prepared-pagination-fresh", "generation_bound_pagination", "fresh", True),
    ("prepared-tied-fresh", "tied_time", "fresh", True),
    ("prepared-tied-idle", "tied_time", "idle", True),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=400)
    args = parser.parse_args()
    if args.messages < 400 or args.messages % 200 or args.messages > 10000:
        parser.error("Use a multiple of 200 between 400 and 10000 for this diagnostic.")
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    manifest = q.read_json(root / "docs/analytics/acceptance-manifest.json")
    manifest["questions"].update(messages=args.messages, samples=3, warmups=1, idle_seconds=0.02)
    source = q.source_context(root)
    results = []
    started = time.monotonic()
    for label, case, state, prepared in CASES:
        path = output / label; path.mkdir()
        selected = deepcopy(manifest)
        if not prepared:
            selected["questions"].pop("preparation", None)
        config = {"subject_directory": str(root), "subject_files": source["files"],
                  "subject_sha256": q.digest(source), "subject": source,
                  "manifest": selected, "mode": "questions", "messages": args.messages,
                  "case": case, "state": state, "repeat": 0, "known_kinds": True,
                  "profile_updates": False, "output": str(path / "collector"),
                  "data": str(path / "collector/data"),
                  "entry_point": str(root / "tools/qualify_analytics_baseline.py"),
                  "job": f"questions/constrained-windows-8g/{case}/{state}",
                  "profile": "constrained-windows-8g", "hardware": None,
                  "semantic_questions": False}
        if prepared:
            config["question_baselines"] = str(output / "question-baselines")
        q.write_once(path / "input.json", config)
        (path / "owner").mkdir()
        receipt = supervise([sys.executable, config["entry_point"], "--collector-worker", str(path / "input.json")],
                            root, path / "owner", 240, limits=manifest["limits"])
        q.write_once(path / "supervisor.json", receipt)
        payload_path = path / "collector/payload.json"
        payload = q.read_json(payload_path) if payload_path.exists() else {}
        row = {"label": label, "status": receipt["status"], "seconds": receipt.get("seconds"),
               "worker_joined": receipt.get("worker_joined"), "complete": payload.get("complete"),
               "errors": q.check_payload(selected, {}, config["job"], payload),
               "preparation_seconds": payload.get("preparation_seconds"),
               "cold_readiness_seconds": payload.get("cold_readiness_seconds"),
               "initial_builds": payload.get("prepared_input", {}).get("initial_builds_this_job", 1),
               "baseline_id": payload.get("prepared_input", {}).get("baseline_id"),
               "copy_seconds": payload.get("prepared_input", {}).get("copy", {}).get("seconds"),
               "reference_mode": payload.get("verification", {}).get("reference_mode"),
               "verification_seconds": payload.get("verification", {}).get("verification_timings", {}).get("total_seconds"),
               "statistics": payload.get("statistics"), "independent_verification": payload.get("verification"),
               "working_copy_cleanup": payload.get("working_copy_cleanup"),
               "private_process": payload.get("process_instance")}
        results.append(row)
        print(json.dumps(row), flush=True)
        if receipt["status"] != "PASS" or not row["complete"] or row["errors"]:
            break
    passed = len(results) == len(CASES) and all(r["status"] == "PASS" and not r["errors"] for r in results)
    if passed:
        expected = results[0]["independent_verification"]["expected"]
        for row in results[1:4]:
            if row["independent_verification"]["expected"] != expected:
                passed = False
        # Exactly two initial preparations for the seven prepared jobs, with a
        # genuinely distinct process identity for every question sample set.
        passed = (passed and sum(r["initial_builds"] for r in results[1:]) == 2
                  and len({r["private_process"] for r in results}) == len(results))
    result = {"status": "PASS" if passed else "FAIL", "kind": "development_diagnostic",
              "qualification_credit": 0, "messages": args.messages, "measured_samples_per_job": 3,
              "idle_seconds": 0.02, "source_context_sha256": q.digest(source),
              "source_working_tree": source.get("working_tree"), "seconds": time.monotonic() - started,
              "results": results}
    q.write_once(output / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "results"}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
