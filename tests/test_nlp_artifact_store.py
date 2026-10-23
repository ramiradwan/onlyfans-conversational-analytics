"""Real filesystem integrity, publication and ownership boundaries for data bytes."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.nlp import artifact_store as storage
from app.nlp.profiles import AnalysisProfile, Artifact, artifact_closure_digest
from app.persistence.private_files import PrivateFileSecurityError

pytestmark = pytest.mark.ci_tier("fast")
FIXTURES = Path(__file__).resolve().parents[1] / "shared/nlp/v2/fixtures"


def profile_from(document):
    for component in document["components"]:
        closure = tuple(Artifact.model_validate_json(json.dumps(asset)) for asset in document["artifacts"]
                        if asset["artifact_id"] in component["artifact_ids"] or asset["role"] in ("engine", "runtime"))
        component["artifact_closure_sha256"] = artifact_closure_digest(closure)
        component["tokenizer_sha256"] = artifact_closure_digest(tuple(a for a in closure if a.role == "tokenizer"))
    document["resources"]["installed_bytes"] = max(1024, sum(a["size_bytes"] for a in document["artifacts"]))
    return AnalysisProfile.model_validate_json(json.dumps(document))


def example(kind="joint", *, same_weights=False, large=False):
    document = json.loads((FIXTURES / (kind + "-profile.json")).read_text("utf-8"))
    payloads = {}
    for asset in document["artifacts"]:
        if asset["role"] not in storage.DATA_ROLES:
            continue
        name = "shared-content" if same_weights and asset["role"] == "weights" else asset["artifact_id"]
        payload = (name + " opaque bytes").encode()
        if large and asset["role"] == "weights":
            payload = b"x" * (storage.CHUNK_BYTES + 13)
        payloads[asset["artifact_id"]] = payload
        asset.update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    return profile_from(document), payloads


def store(tmp_path, *, closure=4 * 1024 * 1024, total=16 * 1024 * 1024, reserve=0):
    return storage.ArtifactStore(tmp_path / "data", storage.StoreLimits(closure, total, reserve))


def streams(payloads):
    return [(key, BytesIO(value)) for key, value in payloads.items()]


def snapshot(path):
    return {str(member.relative_to(path)): member.read_bytes()
            for member in path.rglob("*") if member.is_file()}


def assert_empty_store(target):
    assert target.root.exists()
    assert list(target.root.iterdir()) == []


@pytest.mark.parametrize("kind", ["joint", "split"])
def test_complete_profile_data_roundtrip_uses_existing_artifact_contracts(tmp_path, kind):
    profile, payloads = example(kind)
    target = store(tmp_path)
    inputs = streams(payloads)
    available = target.stage(profile, inputs)
    assert available.profile_sha256 == profile.identity_digest()
    assert available.data_closure_sha256 == artifact_closure_digest(tuple(a for a in profile.artifacts if a.role in storage.DATA_ROLES))
    assert available.directory.name == available.data_closure_sha256
    assert {entry.artifact.artifact_id for entry in available.artifacts} == set(payloads)
    assert all(entry.path.read_bytes() == payloads[entry.artifact.artifact_id] for entry in available.artifacts)
    assert len(list(available.directory.iterdir())) == len(payloads) + 1
    declarations = json.loads((available.directory / "artifacts.json").read_bytes())
    assert declarations == [entry.artifact.model_dump(mode="json") for entry in available.artifacts]
    assert available.size_bytes == sum(len(x) for x in payloads.values()) + (available.directory / "artifacts.json").stat().st_size
    assert all(not stream.closed for _, stream in inputs)
    assert target.reopen(profile) == available
    assert {path.name for path in target.root.iterdir()} == {available.data_closure_sha256}
    assert not hasattr(available, "trusted") and not hasattr(available, "active")


def test_same_data_reopens_across_profile_and_engine_revisions(tmp_path):
    profile, payloads = example("split")
    target = store(tmp_path)
    first = target.stage(profile, streams(payloads))
    before = snapshot(target.root)
    document = profile.model_dump(mode="json")
    document["revision"] = "next"
    document["artifacts"].reverse()
    for component in document["components"]:
        component["engine"]["engine_version"] = "next"
        component["engine"]["engine_build_sha256"] = "f" * 64
    revised = profile_from(document)
    assert revised.identity_digest() != profile.identity_digest()
    later = target.reopen(revised)
    assert later.directory == first.directory and later.data_closure_sha256 == first.data_closure_sha256
    assert later.profile_sha256 == revised.identity_digest()
    assert snapshot(target.root) == before


def test_shared_id_once_but_identical_bytes_with_distinct_ids_count_separately(tmp_path):
    profile, payloads = example("split", same_weights=True)
    result = store(tmp_path).stage(profile, streams(payloads))
    weights = [entry for entry in result.artifacts if entry.artifact.role == "weights"]
    assert len(weights) == 2 and weights[0].artifact.sha256 == weights[1].artifact.sha256
    assert weights[0].path != weights[1].path
    assert all(entry.path.stat().st_nlink == 1 for entry in weights)
    assert len([a for a in result.artifacts if a.artifact.role == "tokenizer"]) == 1
    assert result.size_bytes == sum(len(x) for x in payloads.values()) + (result.directory / "artifacts.json").stat().st_size


def test_configuration_is_part_of_the_required_data_closure(tmp_path):
    profile, payloads = example()
    document = profile.model_dump(mode="json")
    payload = b'{"setting":1}'
    document["artifacts"].append(dict(
        artifact_id="configuration", role="configuration", format_id="json",
        sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload),
    ))
    document["components"][0]["artifact_ids"].append("configuration")
    profile = profile_from(document)
    target = store(tmp_path)
    with pytest.raises(storage.ArtifactStoreError, match="membership"):
        target.stage(profile, streams(payloads))
    payloads["configuration"] = payload
    result = target.stage(profile, streams(payloads))
    assert any(entry.artifact.role == "configuration" for entry in result.artifacts)


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "engine", "runtime"])
def test_exact_membership_and_executable_role_refusal(tmp_path, fault):
    profile, payloads = example("split")
    target = store(tmp_path)
    inputs = streams(payloads)
    if fault == "missing":
        inputs.pop()
    elif fault == "duplicate":
        inputs.append((inputs[0][0], BytesIO(payloads[inputs[0][0]])))
    else:
        inputs.append((fault, BytesIO(b"unused")))
    with pytest.raises(storage.ArtifactStoreError, match="membership"):
        target.stage(profile, inputs)
    assert_empty_store(target)


@pytest.mark.parametrize("fault", ["short", "long", "hash", "nonbytes", "oversized_read"])
def test_stream_bounds_hash_and_shape_fail_without_partial_publication(tmp_path, fault):
    profile, payloads = example()
    target = store(tmp_path)
    key = next(iter(payloads))
    value = payloads[key]

    class InvalidStream:
        def read(self, size):
            return "not bytes" if fault == "nonbytes" else b"z" * (size + 1)

    inputs = streams(payloads)
    if fault == "short":
        replacement = value[:-1]
    elif fault == "long":
        replacement = value + b"!"
    else:
        replacement = b"z" * len(value)
    inputs[0] = (key, InvalidStream() if fault in ("nonbytes", "oversized_read") else BytesIO(replacement))
    with pytest.raises(storage.ArtifactStoreError, match="size|hash"):
        target.stage(profile, inputs)
    assert_empty_store(target)


def test_stream_short_reads_are_valid_and_requests_are_bounded(tmp_path):
    profile, payloads = example()

    class ShortReader(BytesIO):
        def read(self, size):
            assert 0 < size <= storage.CHUNK_BYTES
            return super().read(min(size, 3))
    result = store(tmp_path).stage(
        profile, [(key, ShortReader(value)) for key, value in payloads.items()],
    )
    assert len(result.artifacts) == len(payloads)


def test_cancellation_before_access_and_between_chunks(tmp_path):
    profile, payloads = example(large=True)
    target = store(tmp_path)
    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled()

    with pytest.raises(Cancelled):
        target.stage(profile, streams(payloads), cancellation_check=cancel)
    assert not target.root.exists()
    cancelled = False

    class Reader(BytesIO):
        def read(self, size):
            nonlocal cancelled
            value = super().read(size)
            cancelled = True
            return value

    def check():
        if cancelled:
            raise Cancelled()

    with pytest.raises(Cancelled):
        target.stage(
            profile, [(key, Reader(value)) for key, value in payloads.items()],
            cancellation_check=check,
        )
    assert_empty_store(target)


def test_metadata_overhead_and_existing_closures_count_toward_quotas(tmp_path):
    profile, payloads = example()
    too_small = store(tmp_path, closure=sum(map(len, payloads.values())))
    with pytest.raises(storage.ArtifactStoreError, match="closure_quota"):
        too_small.stage(profile, streams(payloads))
    assert not too_small.root.exists()
    target = store(tmp_path)
    first = target.stage(profile, streams(payloads))
    before = snapshot(target.root)
    document = profile.model_dump(mode="json")
    for asset in document["artifacts"]:
        if asset["role"] == "weights":
            payloads[asset["artifact_id"]] += b"new"
            value = payloads[asset["artifact_id"]]
            asset.update(size_bytes=len(value), sha256=hashlib.sha256(value).hexdigest())
    revised = profile_from(document)
    limited = storage.ArtifactStore(
        target.root, storage.StoreLimits(4 * 1024 * 1024, first.size_bytes, 0),
    )
    with pytest.raises(storage.ArtifactStoreError, match="store_quota"):
        limited.stage(revised, streams(payloads))
    assert snapshot(target.root) == before


def test_disk_reserve_checked_before_input_and_after_read(tmp_path, monkeypatch):
    profile, payloads = example()
    target = store(tmp_path, reserve=100)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100))

    class Unread:
        def read(self, _):
            raise AssertionError("input must not be read")

    with pytest.raises(storage.ArtifactStoreError, match="disk_reserve"):
        target.stage(profile, [(key, Unread()) for key in payloads])
    assert_empty_store(target)
    free = 10**8
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))

    class Reader(BytesIO):
        def read(self, size):
            nonlocal free
            value = super().read(size)
            free = 100
            return value

    with pytest.raises(storage.ArtifactStoreError, match="disk_reserve"):
        target.stage(profile, [(key, Reader(value)) for key, value in payloads.items()])
    assert_empty_store(target)


@pytest.mark.parametrize("fault", ["bytes", "missing", "extra", "declarations"])
def test_existing_content_is_reverified_not_repaired(tmp_path, fault):
    profile, payloads = example()
    target = store(tmp_path)
    result = target.stage(profile, streams(payloads))
    member = result.artifacts[0].path
    if fault == "bytes":
        member.write_bytes(b"z" * member.stat().st_size)
    elif fault == "missing":
        member.unlink()
    elif fault == "extra":
        (result.directory / "unexpected").write_bytes(b"preserve")
    else:
        metadata = result.directory / "artifacts.json"
        metadata.write_bytes(b" " * metadata.stat().st_size)
    before = snapshot(target.root)
    with pytest.raises((storage.ArtifactStoreError, FileNotFoundError)):
        target.reopen(profile)
    assert snapshot(target.root) == before


def test_existing_directory_cannot_be_overwritten_by_stage(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)
    target.stage(profile, streams(payloads))
    before = snapshot(target.root)
    with pytest.raises(storage.ArtifactStoreError, match="exists"):
        target.stage(profile, streams(payloads))
    assert snapshot(target.root) == before


def test_failure_before_atomic_publish_leaves_previous_closure_unchanged(tmp_path, monkeypatch):
    profile, payloads = example()
    target = store(tmp_path)
    target.stage(profile, streams(payloads))
    before = snapshot(target.root)
    other, inputs = example("split")

    def refuse(source, destination):
        assert source.name.startswith(".stage-") and not destination.exists()
        assert not any(path.name == destination.name for path in target.root.iterdir())
        raise OSError("publication failed")

    monkeypatch.setattr(storage.os, "rename", refuse)
    with pytest.raises(OSError, match="publication failed"):
        target.stage(other, streams(inputs))
    assert snapshot(target.root) == before
    assert not any(path.name.startswith(".stage-") for path in target.root.iterdir())


def test_flush_failure_after_publish_preserves_complete_reopenable_data(tmp_path, monkeypatch):
    profile, payloads = example()
    target = store(tmp_path)
    sync = storage.sync_directory

    def fail_parent(path):
        if path == target.root:
            raise OSError("flush failed")
        return sync(path)

    monkeypatch.setattr(storage, "sync_directory", fail_parent)
    with pytest.raises(OSError, match="flush failed"):
        target.stage(profile, streams(payloads))
    assert not any(
        path.name.startswith(".stage-") or path.name == ".write.lock"
        for path in target.root.iterdir()
    )
    assert target.reopen(profile).artifacts


def test_cleanup_never_deletes_other_staging_or_existing_lock(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)
    target.root.mkdir()
    storage.apply_private_directory_security(target.root)
    other = target.root / (".stage-" + "f" * 32)
    other.mkdir()
    storage.apply_private_directory_security(other)
    (other / "keep").write_bytes(b"untouched")
    storage.apply_private_file_security(other / "keep")
    with pytest.raises(storage.ArtifactStoreError):
        target.stage(profile, [])
    assert (other / "keep").read_bytes() == b"untouched"
    assert list(target.root.iterdir()) == [other]
    lock = target.root / ".write.lock"
    lock.write_bytes(b"held")
    with pytest.raises(storage.ArtifactStoreError, match="busy"):
        target.stage(profile, streams(payloads))
    assert lock.read_bytes() == b"held"


def test_existing_unprivate_root_refused_without_changing_foreign_access(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)
    target.root.mkdir()
    if os.name != "nt":
        target.root.chmod(0o755)
    foreign = target.root / (".stage-" + "f" * 32)
    foreign.mkdir()
    member = foreign / "keep"
    member.write_bytes(b"untouched")
    before_root = target.root.stat()
    before_foreign = foreign.stat()
    before_member = member.stat()

    class Unread:
        def read(self, _):
            raise AssertionError("private root refusal must precede input reads")

    # Exercise the real platform verifier and then the store's use of it.
    with pytest.raises(PrivateFileSecurityError):
        storage.apply_private_directory_security(target.root, repair_permissions=False)
    with pytest.raises(PrivateFileSecurityError):
        target.stage(profile, [(key, Unread()) for key in payloads])
    assert list(target.root.iterdir()) == [foreign]
    assert list(foreign.iterdir()) == [member]
    assert member.read_bytes() == b"untouched"
    assert target.root.stat().st_ino == before_root.st_ino
    assert foreign.stat().st_ino == before_foreign.st_ino
    assert member.stat().st_ino == before_member.st_ino
    if os.name != "nt":
        assert target.root.stat().st_mode == before_root.st_mode


def test_cleanup_preserves_unknown_file_inside_own_staging(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)

    class Reader(BytesIO):
        def read(self, size):
            stage = next(path for path in target.root.iterdir() if path.name.startswith(".stage-"))
            (stage / "unknown").write_bytes(b"preserve")
            raise OSError("read failed")

    with pytest.raises(OSError):
        target.stage(profile, [(key, Reader(value)) for key, value in payloads.items()])
    stage = next(path for path in target.root.iterdir() if path.name.startswith(".stage-"))
    assert (stage / "unknown").read_bytes() == b"preserve"
    assert [path.name for path in stage.iterdir()] == ["unknown"]
    assert not (target.root / ".write.lock").exists()


def test_nested_store_operation_refused_until_lock_released(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)
    observed = False

    class Reader(BytesIO):
        def read(self, size):
            nonlocal observed
            if not observed:
                with pytest.raises(storage.ArtifactStoreError, match="busy"):
                    target.reopen(profile)
                observed = True
            return super().read(size)

    target.stage(profile, [(key, Reader(value)) for key, value in payloads.items()])
    assert observed and target.reopen(profile).artifacts


def test_logical_ids_never_become_paths_or_windows_device_names(tmp_path):
    profile, payloads = example()
    document = profile.model_dump(mode="json")
    old = next(asset["artifact_id"] for asset in document["artifacts"] if asset["role"] == "weights")
    new = "CON:case."
    for asset in document["artifacts"]:
        if asset["artifact_id"] == old:
            asset["artifact_id"] = new
    for component in document["components"]:
        component["artifact_ids"] = [new if value == old else value for value in component["artifact_ids"]]
    payloads[new] = payloads.pop(old)
    result = store(tmp_path).stage(profile_from(document), streams(payloads))
    assert all(len(entry.path.name) == 69 and entry.path.suffix == ".data" for entry in result.artifacts)
    assert not any("CON" in path.name for path in result.directory.iterdir())


@pytest.mark.parametrize("fault", ["copy", "construct", "nested"])
def test_unchecked_profiles_are_revalidated_before_filesystem_effects(tmp_path, fault):
    profile, payloads = example()
    target = store(tmp_path)
    if fault == "copy":
        bad = profile.model_copy(update={"revision": "../outside"})
    elif fault == "construct":
        bad = AnalysisProfile.model_construct(**{**dict(profile), "bindings": ()})
    else:
        artifacts = list(profile.artifacts)
        artifacts[-1] = artifacts[-1].model_copy(update={"size_bytes": -1})
        bad = profile.model_copy(update={"artifacts": tuple(artifacts)})
    with pytest.raises(ValidationError):
        target.stage(bad, streams(payloads))
    assert not target.root.exists()


@pytest.mark.parametrize("root", ["relative", "parent"])
def test_root_path_aliases_refused(tmp_path, root):
    path = Path("relative") if root == "relative" else tmp_path / "sub" / ".." / "escape"
    with pytest.raises(storage.ArtifactStoreError):
        storage.ArtifactStore(path, storage.StoreLimits(1024, 4096, 0))


def test_hardlinked_existing_artifact_refused_without_modifying_other_file(tmp_path):
    profile, payloads = example()
    target = store(tmp_path)
    result = target.stage(profile, streams(payloads))
    member = result.artifacts[0].path
    outside = tmp_path / "outside"
    outside.write_bytes(member.read_bytes())
    member.unlink()
    os.link(outside, member)
    with pytest.raises(storage.ArtifactStoreError, match="file"):
        target.reopen(profile)
    assert outside.read_bytes() == payloads[result.artifacts[0].artifact.artifact_id]


@pytest.mark.parametrize("location", ["root", "closure", "member"])
def test_real_symlinks_refused(tmp_path, location):
    profile, payloads = example()
    target = store(tmp_path)
    if location == "root":
        outside = tmp_path / "outside"
        outside.mkdir()
        link = target.root
        is_directory = True
    else:
        result = target.stage(profile, streams(payloads))
        if location == "closure":
            outside = tmp_path / "outside"
            os.rename(result.directory, outside)
            link = result.directory
            is_directory = True
        else:
            link = result.artifacts[0].path
            outside = tmp_path / "outside"
            outside.write_bytes(link.read_bytes())
            link.unlink()
            is_directory = False
    try:
        link.symlink_to(outside, target_is_directory=is_directory)
    except OSError:
        pytest.skip("symlink creation requires host support")
    before = outside.stat()
    with pytest.raises(PrivateFileSecurityError):
        target.reopen(profile)
    assert outside.stat().st_ino == before.st_ino and link.is_symlink()


def test_windows_reparse_attribute_refused_before_reuse(tmp_path, monkeypatch):
    profile, payloads = example()
    target = store(tmp_path)
    result = target.stage(profile, streams(payloads))
    member = result.artifacts[0].path
    original = os.lstat

    def flagged(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if Path(path) == member:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(os, "lstat", flagged)
    with pytest.raises(PrivateFileSecurityError):
        target.reopen(profile)
    assert member.read_bytes() == payloads[result.artifacts[0].artifact.artifact_id]


@pytest.mark.parametrize("limit", ["directories", "members"])
def test_count_ceiling_refused_before_consuming_streams(tmp_path, limit):
    profile, payloads = example()
    target = store(tmp_path)
    target.root.mkdir()
    storage.apply_private_directory_security(target.root)
    count = storage.MAX_CLOSURE_DIRECTORIES if limit == "directories" else 1
    for index in range(count):
        directory = target.root / (".stage-" + f"{index:032x}")
        directory.mkdir()
        storage.apply_private_directory_security(directory)
    if limit == "members":
        for index in range(storage.MAX_DIRECTORY_FILES + 1):
            path = directory / f"member-{index}"
            path.write_bytes(b"untouched")
            storage.apply_private_file_security(path)

    class Unread:
        def read(self, _):
            raise AssertionError("count refusal must precede input reads")

    previous_names = {path.name for path in target.root.iterdir()}
    previous_bytes = snapshot(target.root)
    reason = "quota" if limit == "directories" else "membership"
    with pytest.raises(storage.ArtifactStoreError, match=reason):
        target.stage(profile, [(key, Unread()) for key in payloads])
    assert {path.name for path in target.root.iterdir()} == previous_names
    assert snapshot(target.root) == previous_bytes


def test_cancellation_during_reopen_preserves_published_bytes(tmp_path, monkeypatch):
    profile, payloads = example()
    target = store(tmp_path)
    available = target.stage(profile, streams(payloads))
    previous = snapshot(target.root)
    original = target._read_file
    verified_artifacts = []

    class Cancelled(Exception):
        pass

    def read_then_cancel(path, *args):
        original(path, *args)
        if path.suffix == ".data":
            verified_artifacts.append(path)

    def check():
        if verified_artifacts:
            raise Cancelled()

    monkeypatch.setattr(target, "_read_file", read_then_cancel)
    with pytest.raises(Cancelled):
        target.reopen(profile, cancellation_check=check)
    assert verified_artifacts
    assert snapshot(target.root) == previous
    assert {path.name for path in target.root.iterdir()} == {available.data_closure_sha256}
    assert target.reopen(profile) == available
