"""Verified, immutable synthetic question inputs, shared across case/state jobs.

A baseline is input data, not a cached test result or a running-process snapshot.
The ordinary, empty and pagination cases share input; only tied_time differs.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
from typing import Callable
from uuid import uuid4

from tools import analytics_qualification as q

SCHEMA = "analytics-question-baseline.v1"
PROTOCOL = "verified-question-inputs.v1"
DATABASES = ("canonical.sqlite3", "analytics.sqlite3", "projections.sqlite3")
MIGRATION_LOCKS = {".bridge-installation-migration.lock", ".projection-migration.lock"}
MIGRATION_DIRECTORIES = {"backups", "projection-backups"}
BLOCK = 1024 * 1024


def require(condition, reason):
    if not condition:
        raise ValueError("question_baseline_" + reason)


def variant(case: str) -> str:
    require(case in {"populated", "empty", "generation_bound_pagination", "tied_time"}, "unknown_case")
    return "tied_time" if case == "tied_time" else "ordinary"


def binding(config: dict, runtime: dict) -> dict:
    return {"protocol": PROTOCOL, "subject_sha256": config["subject_sha256"],
            "manifest_sha256": q.digest(config["manifest"]), "runtime_sha256": q.digest(runtime),
            "variant": variant(config["case"]),
            "messages": config["messages"], "known_kinds": config["known_kinds"]}


def safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            require(not stat.S_ISLNK(info.st_mode) and
                    not getattr(info, "st_file_attributes", 0) & 1024, "path_alias")
    return path


def file_record(path: Path) -> dict:
    safe(path)
    before = path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "file_not_private_regular")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(BLOCK):
            digest.update(data)
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "file_changed_during_read")
    return {"bytes": before.st_size, "sha256": digest.hexdigest()}


def read_record(path: Path) -> dict:
    safe(path)
    require(path.is_file() and path.stat().st_size <= 2 * BLOCK, "manifest_size")
    def unique(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), "duplicate_manifest_key")
        return result
    def nonfinite(value):
        raise ValueError("question_baseline_nonfinite_manifest")
    value = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=nonfinite)
    require(type(value) is dict, "manifest_object")
    return value


def validate_record(record: dict, expected: dict) -> None:
    require(set(record) == {"schema", "binding", "files", "verification", "source_counts",
                           "producer", "build_seconds", "closed_connections", "checkpointed"}, "manifest_fields")
    require(record["schema"] == SCHEMA and record["binding"] == expected, "binding_mismatch")
    require(record["closed_connections"] is True and record["checkpointed"] is True,
            "database_pair_not_closed")
    require(q.finite(record["build_seconds"]), "build_timing")
    files = record["files"]
    require(type(files) is dict and set(files) == set(DATABASES), "database_set")
    for row in files.values():
        require(type(row) is dict and set(row) == {"bytes", "sha256"}
                and type(row["bytes"]) is int and row["bytes"] > 0
                and isinstance(row["sha256"], str) and len(row["sha256"]) == 64
                and all(c in "0123456789abcdef" for c in row["sha256"]), "file_record")
    check = record["verification"]
    require(type(check) is dict and check.get("independent_rebuild_equal") is True
            and check.get("persisted_content_revalidated") is True
            and check.get("expected") == check.get("actual")
            and set(check.get("expected", {})) ==
                {"canonical_content_digest", "projection_digest", "graph_digest"}, "independent_verification")
    require(record["source_counts"] == {"messages": expected["messages"], "revision": 1}, "source_counts")
    require(isinstance(record["producer"], dict) and record["producer"].get("instance")
            and record["producer"].get("subject_sha256") == expected["subject_sha256"]
            and q.digest(record["producer"].get("runtime")) == expected["runtime_sha256"], "producer_missing")


def exact_files(directory: Path):
    require({p.name for p in directory.iterdir()} == set(DATABASES) | {"baseline.json"}, "unexpected_baseline_files")
    for path in directory.iterdir():
        require(path.is_file(), "unexpected_baseline_directory")
        safe(path)


@contextmanager
def exclusive(root: Path, key: str):
    """Busy or abandoned preparation refuses; never steal another producer's lock."""
    lock = root / (key + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise ValueError("question_baseline_preparation_busy") from error
    try:
        yield
    finally:
        lock.rmdir()


def prepare(root: Path, expected: dict, producer: dict,
            build: Callable[[Path], tuple[dict, dict]]) -> tuple[Path, dict, bool]:
    root = safe(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = q.digest(expected)
    target = root / key
    with exclusive(root, key):
        if target.exists():
            exact_files(target)
            record = read_record(target / "baseline.json")
            validate_record(record, expected)
            return target, record, False
        staging = root / (key + ".preparing-" + uuid4().hex)
        staging.mkdir(mode=0o700)
        started = time.monotonic()
        # build must close all product work, checkpoint both DBs and confirm no live
        # connection before returning. An exception leaves diagnostic staging, not a cache hit.
        verification, counts = build(staging)
        require({p.name for p in staging.iterdir()} == set(DATABASES), "unclosed_sidecars_or_extra_files")
        files = {name: file_record(staging / name) for name in DATABASES}
        record = {"schema": SCHEMA, "binding": expected, "files": files,
                  "verification": verification, "source_counts": counts, "producer": producer,
                  "build_seconds": time.monotonic() - started,
                  "closed_connections": True, "checkpointed": True}
        record = json.loads(q.encoded(record))  # Freeze JSON types, including runtime dependency tuples.
        validate_record(record, expected)
        q.write_once(staging / "baseline.json", record)
        # No workload ever opens these baseline DBs. Jobs copy them to private paths.
        for path in staging.iterdir():
            path.chmod(stat.S_IRUSR)
        require(not target.exists(), "concurrent_baseline_publication")
        staging.rename(target)
        return target, record, True


def clone(directory: Path, record: dict, destination: Path) -> dict:
    directory, destination = safe(directory), safe(destination)
    exact_files(directory)
    require(not destination.exists(), "working_copy_already_exists")
    require(not destination.is_relative_to(directory) and not directory.is_relative_to(destination),
            "working_copy_overlaps_baseline")
    require(read_record(directory / "baseline.json") == record, "manifest_changed")
    destination.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    for name in DATABASES:
        source, target = directory / name, destination / name
        info = source.stat()
        require(info.st_nlink == 1 and stat.S_ISREG(info.st_mode), "baseline_link")
        measured, length = hashlib.sha256(), 0
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            while data := incoming.read(BLOCK):
                outgoing.write(data); measured.update(data); length += len(data)
            outgoing.flush(); os.fsync(outgoing.fileno())
        require({"bytes": length, "sha256": measured.hexdigest()} == record["files"][name],
                "working_copy_hash_mismatch")
        require(not os.path.samefile(source, target) and target.stat().st_nlink == 1, "working_copy_not_independent")
        after = source.stat()
        require((info.st_ino, info.st_size, info.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), "baseline_changed_during_copy")
    require(read_record(directory / "baseline.json") == record, "manifest_changed_during_copy")
    token = uuid4().hex
    q.write_once(destination / ".baseline-copy.json", {"token": token, "destination": str(destination),
                 "baseline_manifest_sha256": q.digest(record)})
    return {"ownership_token": token, "seconds": time.monotonic() - started, "bytes": sum(r["bytes"] for r in record["files"].values()),
            "independent_working_copy": True, "all_input_hashes_verified": True}


def build_input(config: dict, directory: Path, progress) -> tuple[dict, dict]:
    from tools.analytics_qualification_fixture import Workload
    case = "tied_time" if variant(config["case"]) == "tied_time" else "populated"
    work = None
    try:
        with progress.phase("baseline.fixture"):
            work = Workload(directory, config["manifest"], config["messages"],
                            question_case=case, known_kinds=config["known_kinds"])
        work.qualification_progress = progress
        with progress.phase("baseline.build_and_publish"):
            candidate = work.f.pipeline.build_candidate(work.account, force=True)
            work.f.pipeline.publish_candidate(candidate)
        verification, counts = work.verify(), work.counts()
        with progress.phase("baseline.close_and_checkpoint"):
            work.close_for_snapshot()
        # The repository factory also maintains a small legacy-projection DB.
        # Copy that schema with the input instead of recreating it for every job.
        # Initial migration backups/logs are retained once, not copied into jobs.
        extras = {p.name for p in directory.iterdir()} - set(DATABASES)
        require(extras <= MIGRATION_LOCKS | MIGRATION_DIRECTORIES, "unexpected_preparation_artifacts")
        if extras:
            retained = directory.with_name(directory.name + ".migration-evidence")
            retained.mkdir()
            for name in sorted(extras):
                item = safe(directory / name)
                if item.is_dir():
                    for child in item.rglob("*"):
                        safe(child)
                item.rename(retained / name)
            progress.record("baseline.migration_evidence", "retained", path=str(retained))
        return verification, counts
    finally:
        if work is not None:
            work.close()


def acquire(config: dict, producer: dict, progress) -> dict:
    expected = binding(config, producer["runtime"])
    with progress.phase("baseline.prepare_or_reuse"):
        directory, record, created = prepare(Path(config["question_baselines"]), expected,
                                            dict(producer, prepared_profile=config["profile"]),
                                            lambda path: build_input(config, path, progress))
    if created:
        # Workload observation hooks form cycles. Do not retain a complete build
        # in the parent while a fresh query process opens its independent copy.
        import gc
        gc.collect()
    with progress.phase("baseline.copy"):
        copy = clone(directory, record, Path(config["data"]))
    return {"protocol": PROTOCOL, "baseline_id": q.digest(expected), "created": created,
            "baseline_manifest_sha256": q.digest(record), "baseline": record, "copy": copy,
            "initial_builds_this_job": int(created), "prior_results_reused": False}


def check_input_receipt(data: dict, manifest: dict, profile: str, case: str) -> list[str]:
    try:
        receipt = data["prepared_input"]
        require(type(receipt) is dict and set(receipt) == {"protocol", "baseline_id", "created",
                "baseline_manifest_sha256", "baseline", "copy", "initial_builds_this_job",
                "prior_results_reused"}, "receipt_fields")
        require(receipt["protocol"] == PROTOCOL and receipt["prior_results_reused"] is False,
                "receipt_protocol")
        record = receipt["baseline"]
        expected = {"protocol": PROTOCOL, "subject_sha256": data["subject_sha256"],
                    "manifest_sha256": q.digest(manifest),
                    "runtime_sha256": q.digest(data["collector_process"]["runtime"]),
                    "variant": variant(case),
                    "messages": manifest["questions"]["messages"],
                    "known_kinds": data["fixture_mode"] == "known_synthetic_kinds"}
        validate_record(record, expected)
        require(receipt["baseline_id"] == q.digest(expected)
                and receipt["baseline_manifest_sha256"] == q.digest(record), "receipt_digest")
        require(type(receipt["created"]) is bool and type(receipt["initial_builds_this_job"]) is int
                and receipt["initial_builds_this_job"] == int(receipt["created"]), "build_count")
        require(type(receipt["copy"]) is dict and set(receipt["copy"]) ==
                {"seconds", "bytes", "independent_working_copy", "all_input_hashes_verified", "ownership_token"},
                "copy_fields")
        require(isinstance(receipt["copy"]["ownership_token"], str)
                and len(receipt["copy"]["ownership_token"]) == 32
                and all(c in "0123456789abcdef" for c in receipt["copy"]["ownership_token"])
                and receipt["copy"]["bytes"] == sum(r["bytes"] for r in record["files"].values()), "copy_identity")
        require(data["process_instance"] != data["collector_process"]["instance"]
                and data["process_instance"] != record["producer"]["instance"], "query_process_not_fresh")
        if receipt["created"]:
            require(record["producer"]["instance"] == data["collector_process"]["instance"]
                    and record["producer"].get("prepared_profile") == profile, "build_producer_mismatch")
        require(receipt["copy"]["independent_working_copy"] is True
                and receipt["copy"]["all_input_hashes_verified"] is True
                and q.finite(receipt["copy"]["seconds"]), "copy_evidence")
        require(data.get("working_copy_cleanup") == "removed_after_joined_success", "working_copy_cleanup")
        check = data["verification"]
        require(check.get("independent_rebuild_equal") is True and check.get("persisted_content_revalidated") is True
                and check.get("expected") == check.get("actual")
                and set(check.get("expected", {})) == {"canonical_content_digest", "projection_digest", "graph_digest"},
                "final_verification")
        reuse = case != "generation_bound_pagination" and data["state"] != "mutated"
        require(check.get("reference_mode") == ("verified_baseline" if reuse else "independent_rebuild"),
                "wrong_verification_mode")
        if reuse:
            require(check.get("canonical_rescanned") is True
                    and check.get("baseline_manifest_sha256") == receipt["baseline_manifest_sha256"]
                    and check.get("expected") == record["verification"]["expected"], "reference_not_bound")
        else:
            require(check["expected"]["canonical_content_digest"] !=
                    record["verification"]["expected"]["canonical_content_digest"], "mutated_reference_not_rebuilt")
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        return [str(error) if isinstance(error, ValueError) else "question_baseline_receipt_invalid"]
    return []


def cleanup_working_copy(config: dict, report: dict) -> str:
    """Only a disposable copy after successful joined verification may be removed."""
    require(report.get("complete") is True and report.get("child_exit_code") == 0
            and report.get("scheduler_closed") is True and report.get("detached_workers") == 0
            and report.get("backlog") == 0, "working_copy_not_joined")
    path, baseline_root = safe(Path(config["data"])), safe(Path(config["question_baselines"]))
    require(not path.is_relative_to(baseline_root) and not baseline_root.is_relative_to(path), "cleanup_overlap")
    ownership = read_record(path / ".baseline-copy.json")
    receipt = report["prepared_input"]
    require(ownership == {"token": receipt["copy"]["ownership_token"], "destination": str(path),
                          "baseline_manifest_sha256": receipt["baseline_manifest_sha256"]}, "working_copy_owner")
    for item in path.rglob("*"):
        safe(item)
        if item.is_dir():
            require(item.parent == path and item.name in MIGRATION_DIRECTORIES
                    and not any(item.iterdir()), "unexpected_working_copy_directory")
            continue
        require(item.parent == path and item.is_file() and item.stat().st_nlink == 1
                and (item.name in DATABASES or item.name in MIGRATION_LOCKS or item.name == ".baseline-copy.json" or
                any(item.name == name + suffix for name in DATABASES for suffix in ("-wal", "-shm", "-journal"))),
                "unexpected_working_copy_content")
    shutil.rmtree(path)
    return "removed_after_joined_success"
