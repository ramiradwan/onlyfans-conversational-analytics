"""Content-addressed evidence bundles: copy unchanged baseline/data objects once.

The bundle is a transport format, never a qualification verdict. Original records
and hashes are preserved; normal public verification still decides acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from uuid import uuid4

from tools import analytics_qualification as q
from tools.analytics_qualification_baselines import safe, file_record, read_record

SCHEMA = "analytics-evidence-bundle.v1"
BLOCK = 1024 * 1024
MAX_TOTAL = 64 * 1024 ** 3


def require(condition, reason):
    if not condition:
        raise ValueError("evidence_bundle_" + reason)


def relative(name):
    p = PurePosixPath(name)
    require(isinstance(name, str) and name and not p.is_absolute() and "\\" not in name
            and ":" not in name and all(x not in {"", ".", ".."} and not x.endswith((" ", "."))
                                         for x in name.split("/")), "unsafe_path")
    return p.parts


def digest_name(value):
    require(isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value), "invalid_hash")
    return value


def load_manifest(store: Path, digest: str) -> dict:
    path = safe(store / "manifests" / (digest_name(digest) + ".json"))
    record = read_record(path)
    require(q.digest(record) == digest and record.get("schema") == SCHEMA, "manifest_hash_or_schema")
    require(set(record) == {"schema", "parent", "files", "directories"}, "manifest_fields")
    require(record["parent"] is None or digest_name(record["parent"]), "parent_hash")
    require(type(record["files"]) is dict and len(record["files"]) <= 100000
            and type(record["directories"]) is list
            and len(record["directories"]) == len(set(record["directories"])), "member_set")
    for name, row in record["files"].items():
        relative(name)
        require(type(row) is dict and set(row) == {"bytes", "sha256"}
                and type(row["bytes"]) is int and row["bytes"] >= 0, "member_record")
        digest_name(row["sha256"])
    require(sum(row["bytes"] for row in record["files"].values()) <= MAX_TOTAL, "total_size_limit")
    for name in record["directories"]:
        relative(name)
    require(not set(record["directories"]) & set(record["files"]), "file_directory_collision")
    return record


def object_path(store, digest):
    digest_name(digest)
    return safe(store / "objects" / digest[:2] / digest[2:])


def _copy_verified(source, target, expected):
    require(not target.exists(), "destination_exists")
    measured, length = hashlib.sha256(), 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while chunk := incoming.read(BLOCK):
            length += len(chunk)
            require(length <= expected["bytes"], "object_size")
            measured.update(chunk); outgoing.write(chunk)
        outgoing.flush(); os.fsync(outgoing.fileno())
    require({"bytes": length, "sha256": measured.hexdigest()} == expected, "object_changed")


def export(root: Path, store: Path, parent: str | None = None) -> dict:
    root, store = safe(root), safe(store)
    require(root.is_dir() and not store.is_relative_to(root) and not root.is_relative_to(store), "overlapping_roots")
    old = load_manifest(store, parent) if parent is not None else None
    store.mkdir(parents=True, exist_ok=True)
    lock = store / "export.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise ValueError("evidence_bundle_export_busy") from error
    created, reused, transferred = 0, 0, 0
    try:
        files, directories = {}, []
        for path in sorted(root.rglob("*")):
            safe(path)
            name = path.relative_to(root).as_posix()
            relative(name)
            if path.is_dir():
                directories.append(name); continue
            files[name] = file_record(path)
        require(len(files) <= 100000 and len(q.encoded(files)) <= 2*BLOCK, "manifest_limit")
        require(sum(row["bytes"] for row in files.values()) <= MAX_TOTAL, "total_size_limit")
        if old is not None:
            require(all(files.get(k) == v for k, v in old["files"].items())
                    and set(old["directories"]) <= set(directories), "prior_evidence_changed")
        verified = set()
        for name, row in files.items():
            target = object_path(store, row["sha256"])
            if target.exists():
                if row["sha256"] not in verified:
                    require(file_record(target) == row, "existing_object_corrupt")
                    verified.add(row["sha256"])
                reused += 1; continue
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(target.name + ".pending-" + uuid4().hex)
            _copy_verified(root.joinpath(*relative(name)), pending, row)
            pending.chmod(stat.S_IRUSR)
            pending.rename(target)
            verified.add(row["sha256"]); created += 1; transferred += row["bytes"]
        # Files can only be archived from a closed producer; detect concurrent edits anyway.
        after_files = {p.relative_to(root).as_posix(): file_record(p)
                       for p in sorted(root.rglob("*")) if p.is_file()}
        after_dirs = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_dir()]
        require(after_files == files and after_dirs == directories, "source_changed_during_export")
        record = {"schema": SCHEMA, "parent": parent, "files": files, "directories": directories}
        require(len(q.encoded(record)) <= 2*BLOCK, "manifest_limit")
        digest = q.digest(record)
        manifest = safe(store / "manifests" / (digest + ".json"))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if manifest.exists():
            require(load_manifest(store, digest) == record, "existing_manifest_changed")
        else:
            q.write_once(manifest, record)
        return {"manifest_sha256": digest, "manifest": str(manifest), "new_objects": created,
                "reused_files": reused, "new_object_bytes": transferred,
                "logical_bytes": sum(row["bytes"] for row in files.values()),
                "qualification_credit": 0}
    finally:
        lock.rmdir()


def restore(store: Path, digest: str, destination: Path) -> dict:
    store, destination = safe(store), safe(destination)
    require(not destination.exists() and not destination.is_relative_to(store)
            and not store.is_relative_to(destination), "restore_destination")
    record = load_manifest(store, digest)
    # Check continuity using the parent manifest, without copying any historical data again.
    if record["parent"] is not None:
        parent = load_manifest(store, record["parent"])
        require(all(record["files"].get(k) == v for k,v in parent["files"].items())
                and set(parent["directories"]) <= set(record["directories"]), "prior_evidence_changed")
    for row in record["files"].values():
        require(file_record(object_path(store, row["sha256"])) == row, "restore_object_corrupt")
    destination.mkdir(parents=True)
    for name in record["directories"]:
        safe(destination.joinpath(*relative(name))).mkdir(parents=True, exist_ok=True)
    for name, row in record["files"].items():
        target = safe(destination.joinpath(*relative(name)))
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(object_path(store, row["sha256"]), target, row)
    return {"manifest_sha256": digest, "files": len(record["files"]),
            "all_member_hashes_verified": True, "qualification_credit": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    write = sub.add_parser("export"); write.add_argument("--closure", type=Path, required=True)
    write.add_argument("--store", type=Path, required=True); write.add_argument("--parent")
    read = sub.add_parser("restore"); read.add_argument("--store", type=Path, required=True)
    read.add_argument("--manifest-sha256", required=True); read.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = (export(args.closure, args.store, args.parent) if args.action == "export" else
              restore(args.store, args.manifest_sha256, args.output))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
