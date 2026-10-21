"""Bounded storage of complete profile data bytes, without release admission."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import BinaryIO
import uuid

from app.nlp.profiles import (
    AnalysisProfile, Artifact, MAX_SAFE_INTEGER, artifact_closure_digest, canonical_bytes,
)
from app.persistence.private_files import (
    apply_private_directory_security, apply_private_file_security,
    reject_path_aliases, sync_directory,
)

DATA_ROLES = frozenset({"weights", "tokenizer", "configuration"})
CHUNK_BYTES = 1024 * 1024
MAX_CLOSURE_DIRECTORIES = 256
MAX_DIRECTORY_FILES = 257
_DIRECTORY_NAME = re.compile(r"(?:[0-9a-f]{64}|\.stage-[0-9a-f]{32})\Z")


class ArtifactStoreError(ValueError):
    """A fixed, text-free storage refusal; not a qualification verdict."""


@dataclass(frozen=True)
class StoreLimits:
    maximum_closure_bytes: int
    maximum_store_bytes: int
    minimum_free_bytes: int

    def __post_init__(self):
        for value in (self.maximum_closure_bytes, self.maximum_store_bytes):
            if type(value) is not int or not 0 < value <= MAX_SAFE_INTEGER:
                raise ArtifactStoreError("artifact_store_limits")
        if (type(self.minimum_free_bytes) is not int
                or not 0 <= self.minimum_free_bytes <= MAX_SAFE_INTEGER):
            raise ArtifactStoreError("artifact_store_limits")


@dataclass(frozen=True)
class AvailableArtifact:
    artifact: Artifact
    path: Path


@dataclass(frozen=True)
class AvailableData:
    """Observed byte availability only; grants no trust or execution authority."""
    profile_sha256: str
    data_closure_sha256: str
    directory: Path
    artifacts: tuple[AvailableArtifact, ...]
    size_bytes: int


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _regular(path: Path):
    reject_path_aliases(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ArtifactStoreError("artifact_store_file")
    return metadata


def _directory(path: Path):
    reject_path_aliases(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactStoreError("artifact_store_directory")
    return metadata


def _filename(artifact: Artifact) -> str:
    # Logical IDs are not filesystem names (including case and Windows devices).
    return hashlib.sha256(artifact.artifact_id.encode("utf-8")).hexdigest() + ".data"


def _expected(profile: AnalysisProfile):
    if not isinstance(profile, AnalysisProfile):
        raise ArtifactStoreError("artifact_store_profile")
    # Also reject unchecked model_copy updates before any filesystem effects.
    profile = AnalysisProfile.model_validate_json(canonical_bytes(profile))
    members = tuple(sorted((asset for asset in profile.artifacts if asset.role in DATA_ROLES),
                           key=lambda asset: asset.artifact_id))
    if not members:
        raise ArtifactStoreError("artifact_store_empty")
    # This is the existing artifact-closure encoding, not a new manifest family.
    encoded = json.dumps([asset.model_dump(mode="json") for asset in members], sort_keys=True,
                         ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    size = len(encoded) + sum(asset.size_bytes for asset in members)
    return profile, members, encoded, size


def _names(directory):
    names = set()
    for path in directory.iterdir():
        if len(names) >= MAX_DIRECTORY_FILES:
            raise ArtifactStoreError("artifact_store_membership")
        names.add(path.name)
    return names


class ArtifactStore:
    """An application-owned, flat store; streams and their lifetime belong to callers.

    Every operation acquires an exclusive create-only lock. A stale lock refuses
    access; this class does not recover locks or delete other staging directories.
    Files are opaque data. No model parser, network transport or loader is used.
    """

    def __init__(self, root: Path, limits: StoreLimits):
        path = Path(root)
        if not path.is_absolute() or ".." in path.parts or not isinstance(limits, StoreLimits):
            raise ArtifactStoreError("artifact_store_root")
        self.root = reject_path_aliases(path)
        self.limits = limits

    def _check_root(self, identity):
        if _identity(_directory(self.root)) != identity:
            raise ArtifactStoreError("artifact_store_root_changed")

    @contextmanager
    def _operation(self, check):
        check()
        reject_path_aliases(self.root)
        try:
            self.root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            created = False
        # Repairing an existing parent DACL can remove inherited child access.
        # Only a newly created, still-empty root may be initialized here.
        apply_private_directory_security(self.root, repair_permissions=created)
        root_id = _identity(_directory(self.root))
        lock = self.root / ".write.lock"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except FileExistsError:
            raise ArtifactStoreError("artifact_store_busy") from None
        lock_id = _identity(os.fstat(descriptor))
        try:
            self._check_root(root_id)
            if _identity(_regular(lock)) != lock_id:
                raise ArtifactStoreError("artifact_store_lock_changed")
            apply_private_file_security(lock)
            yield root_id
        finally:
            os.close(descriptor)
            self._check_root(root_id)
            if _identity(_regular(lock)) != lock_id:
                raise ArtifactStoreError("artifact_store_lock_changed")
            lock.unlink()

    def _poll(self, check, root_id, *, writing=0):
        check()
        self._check_root(root_id)
        if shutil.disk_usage(self.root).free < self.limits.minimum_free_bytes + writing:
            raise ArtifactStoreError("artifact_store_disk_reserve")

    def _used_bytes(self, check, root_id):
        used, count = 0, 0
        for entry in self.root.iterdir():
            self._poll(check, root_id)
            if entry.name == ".write.lock":
                continue
            count += 1
            if count > MAX_CLOSURE_DIRECTORIES:
                raise ArtifactStoreError("artifact_store_quota")
            if not _DIRECTORY_NAME.fullmatch(entry.name):
                raise ArtifactStoreError("artifact_store_member")
            _directory(entry)
            for name in _names(entry):
                self._poll(check, root_id)
                used += _regular(entry / name).st_size
                if used > self.limits.maximum_store_bytes:
                    raise ArtifactStoreError("artifact_store_quota")
        return used, count

    def _read_file(self, path, expected_size, expected_digest, check, root_id):
        self._poll(check, root_id)
        before = _regular(path)
        if before.st_size != expected_size:
            raise ArtifactStoreError("artifact_store_size")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or _identity(_regular(path)) != _identity(opened):
                raise ArtifactStoreError("artifact_store_file_changed")
            digest, count = hashlib.sha256(), 0
            while True:
                self._poll(check, root_id)
                block = stream.read(min(CHUNK_BYTES, expected_size - count + 1))
                if not block:
                    break
                count += len(block)
                if count > expected_size:
                    raise ArtifactStoreError("artifact_store_size")
                digest.update(block)
            if count != expected_size or digest.hexdigest() != expected_digest:
                raise ArtifactStoreError("artifact_store_hash")
            after = _regular(path)
            if (_identity(after) != _identity(opened) or after.st_size != expected_size
                    or os.fstat(stream.fileno()).st_size != expected_size):
                raise ArtifactStoreError("artifact_store_file_changed")

    def _verify(self, directory, profile, members, encoded, size, check, root_id):
        self._poll(check, root_id)
        directory_id = _identity(_directory(directory))
        expected = {"artifacts.json", *(_filename(asset) for asset in members)}
        if _names(directory) != expected:
            raise ArtifactStoreError("artifact_store_membership")
        self._read_file(directory / "artifacts.json", len(encoded), hashlib.sha256(encoded).hexdigest(), check, root_id)
        for asset in members:
            self._read_file(directory / _filename(asset), asset.size_bytes, asset.sha256, check, root_id)
        self._poll(check, root_id)
        if (_identity(_directory(directory)) != directory_id
                or _names(directory) != expected):
            raise ArtifactStoreError("artifact_store_directory_changed")
        return AvailableData(profile.identity_digest(), artifact_closure_digest(members), directory,
                             tuple(AvailableArtifact(asset, directory / _filename(asset)) for asset in members), size)

    def _write_file(self, path, source, size, digest, owned, stage_id, check, root_id):
        self._poll(check, root_id)
        reject_path_aliases(path)
        if _identity(_directory(path.parent)) != stage_id:
            raise ArtifactStoreError("artifact_store_staging_changed")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags, 0o600)
        owned[path.name] = _identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as output:
            if _identity(_regular(path)) != owned[path.name]:
                raise ArtifactStoreError("artifact_store_file_changed")
            apply_private_file_security(path)
            actual, count = hashlib.sha256(), 0
            while count < size:
                self._poll(check, root_id)
                request = min(CHUNK_BYTES, size-count)
                block = source.read(request)
                if type(block) is not bytes or not 0 < len(block) <= request:
                    raise ArtifactStoreError("artifact_store_size")
                self._poll(check, root_id, writing=len(block))
                output.write(block)
                count += len(block)
                actual.update(block)
            self._poll(check, root_id)
            extra = source.read(1)
            if type(extra) is not bytes or extra:
                raise ArtifactStoreError("artifact_store_size")
            if actual.hexdigest() != digest:
                raise ArtifactStoreError("artifact_store_hash")
            output.flush()
            os.fsync(output.fileno())
            if _identity(_regular(path)) != owned[path.name]:
                raise ArtifactStoreError("artifact_store_file_changed")

    def _cleanup(self, stage, identity, owned, root_id):
        self._check_root(root_id)
        # This is deliberately flat and identity-checked, never a recursive delete.
        if stage.parent != self.root or _identity(_directory(stage)) != identity:
            raise ArtifactStoreError("artifact_store_staging_changed")
        for name, expected in owned.items():
            member = stage / name
            if _identity(_regular(member)) != expected:
                raise ArtifactStoreError("artifact_store_staging_changed")
            member.unlink()
        stage.rmdir()  # Unknown entries are preserved and prevent removal.

    def reopen(self, profile: AnalysisProfile, *, cancellation_check: Callable[[], None] = lambda: None) -> AvailableData:
        """Rehash a complete closure; never infer availability from its directory name."""
        profile, members, encoded, size = _expected(profile)
        if size > self.limits.maximum_closure_bytes:
            raise ArtifactStoreError("artifact_store_closure_quota")
        with self._operation(cancellation_check) as root_id:
            self._used_bytes(cancellation_check, root_id)
            return self._verify(self.root / artifact_closure_digest(members), profile, members, encoded, size,
                                cancellation_check, root_id)

    def stage(self, profile: AnalysisProfile, streams: Iterable[tuple[str, BinaryIO]], *,
              cancellation_check: Callable[[], None] = lambda: None) -> AvailableData:
        """Publish all data members or none; existing closures require explicit reopen."""
        from io import BytesIO

        profile, members, encoded, size = _expected(profile)
        if size > self.limits.maximum_closure_bytes:
            raise ArtifactStoreError("artifact_store_closure_quota")
        with self._operation(cancellation_check) as root_id:
            used, directories = self._used_bytes(cancellation_check, root_id)
            if used + size > self.limits.maximum_store_bytes or directories >= MAX_CLOSURE_DIRECTORIES:
                raise ArtifactStoreError("artifact_store_quota")
            destination = self.root / artifact_closure_digest(members)
            if destination.exists():
                raise ArtifactStoreError("artifact_store_exists")
            self._poll(cancellation_check, root_id, writing=size)
            stage = self.root / (".stage-" + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            stage_id, owned = _identity(_directory(stage)), {}
            published = False
            try:
                apply_private_directory_security(stage)
                expected = {asset.artifact_id: asset for asset in members}
                seen = set()
                for artifact_id, stream in streams:
                    self._poll(cancellation_check, root_id)
                    if artifact_id not in expected or artifact_id in seen:
                        raise ArtifactStoreError("artifact_store_membership")
                    seen.add(artifact_id)
                    asset = expected[artifact_id]
                    self._write_file(stage / _filename(asset), stream, asset.size_bytes, asset.sha256,
                                     owned, stage_id, cancellation_check, root_id)
                if seen != set(expected):
                    raise ArtifactStoreError("artifact_store_membership")
                self._write_file(stage / "artifacts.json", BytesIO(encoded), len(encoded), hashlib.sha256(encoded).hexdigest(),
                                 owned, stage_id, cancellation_check, root_id)
                self._verify(stage, profile, members, encoded, size, cancellation_check, root_id)
                self._poll(cancellation_check, root_id)
                if _identity(_directory(stage)) != stage_id:
                    raise ArtifactStoreError("artifact_store_staging_changed")
                sync_directory(stage)
                if _identity(_directory(stage)) != stage_id:
                    raise ArtifactStoreError("artifact_store_staging_changed")
                if destination.exists():
                    raise ArtifactStoreError("artifact_store_exists")
                os.rename(stage, destination)
                published = True
                sync_directory(self.root)
                return AvailableData(profile.identity_digest(), artifact_closure_digest(members), destination,
                                     tuple(AvailableArtifact(asset, destination / _filename(asset)) for asset in members), size)
            finally:
                if not published:
                    self._cleanup(stage, stage_id, owned, root_id)
