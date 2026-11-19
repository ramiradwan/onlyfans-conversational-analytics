"""Prepare and verify source-bound, same-run frontend assets for CI only.

The consumer must obtain the artifact from the exact Actions run and the
producer attempt from authoritative job metadata. This never authorizes a
release build, cross-run cache, or reuse of node_modules.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

RECEIPT = "receipt.json"
REQUIRED = {"index.html", "manifest.json", "setup-code/index.html", "onboarding/index.html", "browser-tasks/index.html"}
PRODUCER = "web-build-assets"


class AssetError(ValueError):
    pass


def require(condition, detail):
    if not condition:
        raise AssetError(detail)


def metadata(path):
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "linked asset entry")
    return info


def inventory(root):
    """Hash every asset; reject links, hidden entries and Windows path aliases."""
    root = Path(root)
    require(stat.S_ISDIR(metadata(root).st_mode), "missing asset directory")
    result, aliases = {}, set()
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        parts = name.split("/")
        require(all(re.fullmatch(r"[A-Za-z0-9_@.+-]+", part) and not part.startswith(".")
                    and not part.endswith(".") and part.split(".")[0].upper() not in {
                        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)),
                        *(f"LPT{i}" for i in range(1,10))} for part in parts), "unsafe asset path")
        alias = name.casefold()
        require(alias not in aliases, "case-insensitive asset collision")
        aliases.add(alias)
        info = metadata(path)
        if stat.S_ISDIR(info.st_mode):
            continue
        require(stat.S_ISREG(info.st_mode), "non-regular asset entry")
        with path.open("rb") as stream:
            result[name] = {"bytes": info.st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    require(REQUIRED <= result.keys() and any(name.startswith("assets/") and name.endswith(".js") for name in result),
            "incomplete frontend build")
    return result


def coordinate(source, run, attempt):
    require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source)
            and type(run) is int and run > 0 and type(attempt) is int and attempt > 0, "invalid asset producer identity")
    return {"source_commit": source, "workflow_run_id": run, "run_attempt": attempt, "producer": PRODUCER}


def canonical(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def read_receipt(path):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate asset receipt key")
            result[key] = value
        return result
    info = metadata(path)
    require(stat.S_ISREG(info.st_mode) and info.st_size <= 8 * 1024 * 1024, "invalid asset receipt")
    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def copy_verified(source, destination, files):
    require(not destination.exists() and not destination.is_symlink(), "asset destination already exists")
    destination.mkdir(parents=True)
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    require(inventory(destination) == files, "copied asset bytes differ")


def seal(dist, bundle, *, source, run, attempt):
    dist, bundle = Path(dist), Path(bundle)
    require(not bundle.exists() and not bundle.is_symlink(), "asset bundle already exists")
    files = inventory(dist)
    receipt = {"schema": "ci-frontend-assets/v1", **coordinate(source, run, attempt), "files": files}
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frontend-seal-", dir=bundle.parent) as temp:
        stage = Path(temp) / "bundle"
        copy_verified(dist, stage / "dist", files)
        (stage / RECEIPT).write_bytes(canonical(receipt))
        stage.rename(bundle)
    return receipt


def restore(bundle, destination, *, source, run, attempt):
    bundle, destination = Path(bundle), Path(destination)
    require(stat.S_ISDIR(metadata(bundle).st_mode) and {p.name for p in bundle.iterdir()} == {"dist", RECEIPT},
            "unexpected asset bundle inventory")
    receipt = read_receipt(bundle / RECEIPT)
    expected = {"schema": "ci-frontend-assets/v1", **coordinate(source, run, attempt), "files": inventory(bundle / "dist")}
    require(canonical(receipt) == canonical(expected), "asset source, attempt or digest differs")
    require(not destination.exists() and not destination.is_symlink(), "asset destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frontend-restore-", dir=destination.parent) as temp:
        stage = Path(temp) / "dist"
        copy_verified(bundle / "dist", stage, expected["files"])
        stage.rename(destination)
    return receipt


def compare(first, second):
    a, b = inventory(first), inventory(second)
    return {"equal": a == b, "first_files": len(a), "second_files": len(b),
            "first_bytes": sum(v["bytes"] for v in a.values()), "second_bytes": sum(v["bytes"] for v in b.values()),
            "only_first": sorted(a.keys()-b.keys()), "only_second": sorted(b.keys()-a.keys()),
            "changed": sorted(name for name in a.keys() & b.keys() if a[name] != b[name])}


def successful_producer_attempt(client, *, source, run, current_attempt):
    from tools.engineering_attestation import latest_ci_jobs
    coordinate(source, run, current_attempt)
    latest = latest_ci_jobs(client, run_id=run, run_attempt=current_attempt, source_commit=source)
    job = latest.get(PRODUCER)
    require(isinstance(job, dict) and job.get("status") == "completed" and job.get("conclusion") == "success",
            "current asset producer is not successful")
    return job["run_attempt"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "restore", "compare"))
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--source", default=os.environ.get("PRODUCT_SHA"))
    parser.add_argument("--run-id", type=int, default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--producer-attempt", type=int)
    parser.add_argument("--resolve-producer", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "compare":
            result = compare(args.first, args.second)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["equal"] else 1
        attempt = args.producer_attempt
        if args.resolve_producer:
            require(args.action == "restore" and attempt is None, "producer resolution is only for restore")
            root = Path(__file__).resolve().parents[1]
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from tools.engineering_attestation import GitHubApi, ContractError
            try:
                attempt = successful_producer_attempt(GitHubApi(os.environ["GH_TOKEN"]), source=args.source,
                    run=args.run_id, current_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
            except ContractError as error:
                raise AssetError("asset producer metadata unavailable") from error
        action = seal if args.action == "seal" else restore
        result = action(args.first, args.second, source=args.source, run=args.run_id, attempt=attempt)
        print(f"verified frontend assets: {len(result['files'])} files")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"frontend asset reuse refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
