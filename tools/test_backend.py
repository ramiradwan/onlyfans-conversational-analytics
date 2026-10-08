"""Run the same backend lanes locally and in Actions.

Examples:
  python tools/test_backend.py fast
  python tools/test_backend.py integration --shard 2 -- -x
  python tools/test_backend.py stateful --profile analytics_convergence_fast
  python tools/test_backend.py list windows-analytics
  python tools/test_backend.py list all --validate
  python tools/test_backend.py list windows-full-regression --shard 1 --validate
  python tools/test_backend.py windows-full-regression --shard 2
  python tools/test_backend.py update-windows-manifest --inventory raw/report.json

Use the repository's Python environment and build the frontend before full
collection, just as for bare ``python -m pytest``. Arguments after ``--`` go
unchanged to pytest, so a file/node ID or ``-k`` can narrow a lane locally.
"""

from __future__ import annotations

import argparse
import ast
import json
import locale
import math
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ci_selection import LANES, LEGACY_EXCLUDED, SelectionError, load_manifest
from tools import ci_windows_shards as windows_shards


ALIASES = {"backend-fast": "fast", "analytics-integration": "integration",
           "windows-platform-contract": "windows-platform", "analytics-windows-contract": "windows-analytics",
           "windows-full-regression": "legacy-windows"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("command", nargs="?", help="Lane name, list, update-manifest, or update-windows-manifest")
    result.add_argument("list_lane", nargs="?", help="Lane to inspect after list (default: all)")
    result.add_argument("--lane", choices=(*LANES, *ALIASES), help="Alternative to the positional lane name")
    result.add_argument("--list", dest="list_only", action="store_true", help="Collect and print the selected lane without executing tests")
    result.add_argument("--shard", type=int, choices=range(1, 5))
    result.add_argument("--profile", help="Exact checked-in Hypothesis profile, or analytics_oracle_falsifiers")
    result.add_argument("--output-dir", type=Path, help="JUnit and CI report directory")
    result.add_argument("--manifest", type=Path, default=ROOT / "ci/backend-test-shards.json")
    result.add_argument("--windows-manifest", type=Path, default=ROOT / "ci/windows-full-test-shards.json")
    result.add_argument("--inventory", type=Path, help="Complete raw unsharded Windows collection report for update-windows-manifest")
    result.add_argument("--validate", action="store_true", help="Check full manifest inventory while listing all tests")
    result.add_argument("--rebalance", action="store_true", help="Reassign all integration files, only with measured --timings")
    result.add_argument("--timings", type=Path, help="Downloaded CI report directory, report.json, or file-to-seconds JSON")
    result.add_argument("--dry-run", action="store_true", help="Print the command or manifest diff without running/writing")
    return result


def integration_files(root: Path) -> dict[str, int]:
    """Read explicit declarations without importing tests or starting runtimes."""
    result = {}
    for path in sorted((root / "tests").rglob("test_*.py")):
        if "fixtures" in path.parts or "architecture_invalid" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declarations = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr == "ci_tier"]
        if any(node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "integration"
               for node in declarations):
            result[path.relative_to(root).as_posix()] = max(1, sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                for node in ast.walk(tree)))
    return result


def updated_manifest(manifest: dict[str, Any], files: dict[str, int], *,
                     timings: dict[str, float] | None = None, rebalance: bool = False) -> dict[str, Any]:
    """Keep existing assignments unless a reviewer explicitly requests balancing."""
    result = json.loads(json.dumps(manifest))
    if rebalance and (timings is None or set(files) - set(timings)):
        raise SelectionError("Rebalancing requires measured timing seconds for every integration file")
    if timings is not None and any(not isinstance(value, (float, int)) or isinstance(value, bool)
                                   or value < 0 or not math.isfinite(value) for value in timings.values()):
        raise SelectionError("Timing values must be finite non-negative seconds")
    assignments = {} if rebalance else {path: shard for path, shard in manifest["integration_shards"].items() if path in files}
    weights = timings or files
    fallback = statistics.median(timings.values()) if timings else None
    weight = lambda path: float(weights.get(path, fallback if fallback is not None else files[path]))
    loads = {shard: sum(weight(path) for path, owner in assignments.items() if owner == shard)
             for shard in range(1, 5)}
    pending = set(files) - set(assignments)
    for path in sorted(pending, key=lambda name: (-weight(name), name)):
        shard = min(loads, key=lambda number: (loads[number], number))
        assignments[path] = shard
        loads[shard] += weight(path)
    result["integration_shards"] = dict(sorted(assignments.items()))
    if rebalance:
        result["assignment_basis"] = {"status": "measured", "method": "reviewed file-duration longest-first allocation",
                                      "timing_seconds": dict(sorted(timings.items()))}
    elif assignments != manifest["integration_shards"]:
        result["assignment_basis"]["status"] = "provisional_unmeasured"
        result["assignment_basis"]["method"] = "preserved existing assignments; new files assigned deterministically by declared test counts"
    return result


def lane_name(args: argparse.Namespace) -> str:
    raw_lane = args.lane or (args.list_lane or "all" if args.command == "list" else args.command)
    return ALIASES.get(raw_lane, raw_lane)


def explicit_targets(extra: list[str]) -> list[str]:
    return [value for value in extra if not value.startswith("-")
            and (value.endswith(".py") or ".py::" in value or (ROOT / value).is_dir())]


def build_command(args: argparse.Namespace, extra: list[str], manifest: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    listing = args.command == "list" or args.list_only
    lane = lane_name(args)
    if lane not in LANES:
        raise SelectionError("Choose a lane, for example: python tools/test_backend.py fast")
    if args.list_lane and not listing:
        raise SelectionError("Pytest arguments must follow --")
    if args.shard is not None and lane not in {"integration", "legacy-windows"}:
        raise SelectionError("--shard is only valid for integration or windows-full-regression")
    if lane == "legacy-windows" and args.shard not in (None, 1, 2):
        raise SelectionError("Full Windows regression has exactly two shards: --shard 1 or 2")
    if args.profile is not None and lane != "stateful":
        raise SelectionError("--profile is only valid for stateful")
    if args.validate and (not listing or (lane != "all" and not (lane == "legacy-windows" and args.shard)) or extra):
        raise SelectionError("Use list all --validate or list windows-full-regression --shard N --validate without pytest selection arguments")
    if args.rebalance or args.timings or args.inventory:
        raise SelectionError("--rebalance, --timings and --inventory belong to manifest update commands")
    environment = os.environ.copy()
    environment.pop("HYPOTHESIS_PROFILE", None)
    if lane == "stateful":
        profile = manifest["stateful_profiles"].get(args.profile)
        if profile is None:
            raise SelectionError("Choose --profile from: " + ", ".join(manifest["stateful_profiles"]))
        if profile["hypothesis_profile"]:
            environment["HYPOTHESIS_PROFILE"] = profile["hypothesis_profile"]
        else:
            environment.pop("HYPOTHESIS_PROFILE", None)
    identity = {"fast": "backend-fast", "windows-platform": "windows-platform-contract",
                "windows-analytics": "analytics-windows-contract"}.get(lane, lane)
    if lane == "integration" and args.shard:
        identity = f"analytics-integration-{args.shard}"
    elif lane == "stateful":
        identity = args.profile
    elif lane == "legacy-windows":
        identity = "windows-full-regression" + (f"-{args.shard}" if args.shard else "")
    environment.setdefault("CI_TEST_LANE", identity)
    output = args.output_dir or Path(environment.get("CI_REPORT_DIR", ROOT / "artifacts/ci-tests" / identity))
    command = [sys.executable, "-m", "pytest", "-p", "tools.ci_pytest", "--override-ini=addopts=",
               "--ci-output-dir", str(output),
               "--junitxml", str(output / "junit.xml"), "--durations=50", "--durations-min=0.25"]
    if lane == "legacy-windows":
        command.extend(["-m", windows_shards.LEGACY_WINDOWS_EXPRESSION])
        if args.shard:
            command.extend(["--ci-windows-shard", str(args.shard), "--ci-windows-manifest", str(args.windows_manifest)])
    else:
        command.extend(["--ci-lane", lane, "--ci-manifest", str(args.manifest)])
        if args.shard:
            command.extend(["--ci-shard", str(args.shard)])
    if args.profile:
        command.extend(["--ci-profile", args.profile])
        # Loading a different module registers a different Hypothesis profile;
        # collect only this profile's selectors, as in the original workflow.
        if not explicit_targets(extra):
            command.extend(manifest["stateful_profiles"][args.profile]["selectors"])
    full_inventory = lane != "stateful" and not explicit_targets(extra) and not any(
        value == "--ignore" or value.startswith("--ignore=") or value == "--ignore-glob"
        or value.startswith("--ignore-glob=") for value in extra)
    if (args.validate or full_inventory) and (lane != "legacy-windows" or args.shard):
        command.append("--ci-validate")
    if listing:
        command.extend(["--collect-only", "-q"])
    command.extend(extra)
    return command, environment


def prerequisite_errors(lane: str, profile: str | None, targets: list[str], *, root: Path = ROOT,
                        execute: bool = True) -> list[str]:
    """Explain missing local bootstrap inputs before a long collection fails."""
    errors = []
    full_collection = not targets and lane != "stateful"
    needs_agent = profile in {"agent_tier_a_general", "agent_tier_a_deletion"}
    if full_collection and not (root / "app/static/dist/index.html").is_file():
        errors.append("Frontend build missing: run npm ci --prefix frontend && npm run build --prefix frontend")
    if full_collection and not (root / "extension/dist/manifest.json").is_file():
        errors.append("Extension build missing: run npm ci --prefix extension && npm run build --prefix extension")
    if (full_collection or needs_agent) and not (root / "extension/node_modules").is_dir():
        errors.append("Extension test dependencies missing: run npm ci --prefix extension")
    if (full_collection or needs_agent) and shutil.which("node") is None:
        errors.append("Node.js is missing from PATH: install the CI Node.js 22 runtime and reopen your terminal")
    if (execute and full_collection and lane != "windows-analytics"
            and shutil.which("openssl") is None):
        if os.name == "nt":
            errors.append('OpenSSL is missing from PATH. With Git for Windows installed, run: '
                          '$env:Path = "C:\\Program Files\\Git\\usr\\bin;$env:Path" '
                          '(otherwise install Git for Windows first: winget install --id Git.Git)')
        else:
            errors.append("OpenSSL is missing from PATH; on Ubuntu run sudo apt-get install openssl")
    return errors


def read_timings(path: Path) -> dict[str, float]:
    """Import real phase timings; median repeated samples without mixing OSes."""
    paths = sorted(path.rglob("report.json")) if path.is_dir() else [path]
    samples: dict[str, list[float]] = {}
    for report_path in paths:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SelectionError(f"Expected a timing mapping or CI report object: {report_path}")
        if not path.is_dir() and isinstance(payload, dict) and "schema" not in payload:
            return payload
        if payload.get("schema") != "ci-test-report/v1":
            continue
        if (payload.get("platform") != "Linux" or payload.get("collect_only")
                or payload.get("exit_code") != 0 or not payload.get("complete")
                or payload.get("profile") or payload.get("selection_profile") or payload.get("collection_errors")):
            continue
        if payload.get("partial") and payload.get("lane") not in {
                "legacy-linux", "backend-linux", "build-and-test-test-backend"}:
            continue
        narrowing = payload.get("selection", {})
        if narrowing.get("keyword") or any("::" in target for target in narrowing.get("targets", [])):
            continue
        # Only complete required file populations are useful for balancing.
        # This catches positional parameter/class slices and --deselect even
        # when a producer's generic partial flag did not record them.
        expected_by_file: dict[str, set[str]] = {}
        for row in payload.get("collected", []):
            if not set(row["markers"]) & (LEGACY_EXCLUDED | {"windows_production"}):
                expected_by_file.setdefault(row["nodeid"].split("::", 1)[0], set()).add(row["nodeid"])
        selected_by_file: dict[str, set[str]] = {}
        for nodeid in payload.get("selected", []):
            selected_by_file.setdefault(nodeid.split("::", 1)[0], set()).add(nodeid)
        totals: dict[str, float] = {}
        for phase in payload.get("reports", []):
            file = phase["nodeid"].split("::", 1)[0]
            if not expected_by_file.get(file) or selected_by_file.get(file) != expected_by_file[file]:
                continue
            totals[file] = totals.get(file, 0.0) + float(phase["duration"])
        for file, seconds in totals.items():
            samples.setdefault(file, []).append(seconds)
    if not samples:
        raise SelectionError("No successful Linux execution reports found; collect-only, failed, partial and stateful runs do not supply shard timings")
    return {file: statistics.median(seconds) for file, seconds in sorted(samples.items())}


def run_streaming(command: list[str], environment: dict[str, str], output: Path) -> int:
    """Retain the same diagnostic log locally while streaming normal pytest UI."""
    output.mkdir(parents=True, exist_ok=True)
    # Do not force Python stdio to UTF-8: on Windows that changes descendant
    # writers without changing subprocess.run(text=True)'s locale decoder.
    # The resulting reader-thread UnicodeDecodeError can even yield stdout=None.
    environment = dict(environment, PYTHONUNBUFFERED="1")
    encoding = environment.get("PYTHONIOENCODING", "").split(":", 1)[0] or None
    if encoding is None and environment.get("PYTHONUTF8") in {"0", "1"}:
        encoding = "utf-8" if environment["PYTHONUTF8"] == "1" else locale.getencoding()
    with (output / "run.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding=encoding, errors="replace")
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            return process.wait()
        except KeyboardInterrupt:
            # The child normally receives the console interrupt too. Give
            # pytest time to flush its partial report before stopping our child.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 130
        finally:
            if process.stdout is not None:
                process.stdout.close()


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    split = values.index("--") if "--" in values else len(values)
    args = parser().parse_args(values[:split])
    extra = values[split + 1:] if split < len(values) else []
    try:
        if args.command == "update-windows-manifest":
            if extra or args.lane or args.list_lane or args.shard or args.profile or args.validate or args.list_only or not args.inventory:
                raise SelectionError("update-windows-manifest requires --inventory <raw-unsharded-report.json>; optional --timings, --rebalance, --windows-manifest and --dry-run")
            previous = windows_shards.load_manifest(ROOT, args.windows_manifest) if args.windows_manifest.exists() else None
            inventory = windows_shards.read_inventory(args.inventory)
            timings = windows_shards.read_inventory(args.timings, execution=True) if args.timings else None
            updated = windows_shards.updated_manifest(previous, inventory, timing_report=timings, rebalance=args.rebalance)
            content = json.dumps(updated, indent=2) + "\n"
            if args.dry_run:
                import difflib
                original = args.windows_manifest.read_text(encoding="utf-8") if args.windows_manifest.exists() else ""
                print("".join(difflib.unified_diff(original.splitlines(True), content.splitlines(True),
                                                  fromfile=str(args.windows_manifest), tofile="updated Windows manifest")), end="")
            else:
                args.windows_manifest.write_text(content, encoding="utf-8")
                print(f"Updated {args.windows_manifest}; review owners and assignment_basis.unmeasured_files")
            return 0
        # The exhaustive reference must still run with missing or invalid tier
        # metadata. Loading its manifest would couple the two parity oracles.
        manifest = {} if lane_name(args) == "legacy-windows" else load_manifest(ROOT, args.manifest)
        if args.command == "update-manifest":
            if extra or args.lane or args.list_lane or args.shard or args.profile or args.validate or args.list_only or args.inventory:
                raise SelectionError("update-manifest accepts only --manifest, --timings, --rebalance and --dry-run")
            timings = read_timings(args.timings) if args.timings else None
            updated = updated_manifest(manifest, integration_files(ROOT), timings=timings, rebalance=args.rebalance)
            content = json.dumps(updated, indent=2) + "\n"
            if args.dry_run:
                import difflib
                print("".join(difflib.unified_diff(args.manifest.read_text(encoding="utf-8").splitlines(True),
                                                  content.splitlines(True), fromfile=str(args.manifest), tofile="updated manifest")), end="")
            else:
                args.manifest.write_text(content, encoding="utf-8")
                print(f"Updated {args.manifest}; existing assignments {'rebalanced from measurements' if args.rebalance else 'preserved'}")
            return 0
        command, environment = build_command(args, extra, manifest)
        print(subprocess.list2cmdline(command), flush=True)
        if args.dry_run:
            return 0
        errors = prerequisite_errors(lane_name(args), args.profile, explicit_targets(extra),
                                     execute=not (args.command == "list" or args.list_only))
        if errors:
            raise SelectionError("\n".join(errors))
        output = Path(command[command.index("--ci-output-dir") + 1])
        return run_streaming(command, environment, output)
    except (SelectionError, windows_shards.WindowsShardError, OSError, json.JSONDecodeError) as error:
        print(f"Backend CI selection error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
