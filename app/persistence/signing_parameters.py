"""Auth-store publication checkpoints and disposable private object cache."""
import os
from pathlib import Path
from uuid import uuid4

from app.persistence.private_files import (
    apply_private_file_security, private_file_identity, reject_path_aliases,
    sync_directory, sync_file,
)
from app.security.signing_parameter_publications import canonical, parse, sha256, require, DIGEST, OBJECT_LIMIT


class SigningParameterPersistence:
    def __init__(self, authentication, cache_path):
        self.authentication = authentication
        self.cache_path = reject_path_aliases(cache_path)

    def load(self):
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT state_json, state_sha256, initialized FROM signing_publication_authority WHERE singleton=1").fetchone()
        require(row is not None)
        if row["state_json"] is None:
            require(row["state_sha256"] is None and row["initialized"] == 0)
            return None
        raw = row["state_json"].encode("utf-8")
        require(row["initialized"] == 1)
        require(sha256(raw) == row["state_sha256"])
        return parse(raw, maximum=192 * 1024, signed=True)

    def save(self, state):
        raw = canonical(state)
        require(len(raw) < 192 * 1024)
        with self.authentication.database.transaction() as connection:
            cursor = connection.execute("UPDATE signing_publication_authority SET state_json=?, state_sha256=?, initialized=1 WHERE singleton=1", (raw.decode("utf-8"), sha256(raw)))
            require(cursor.rowcount == 1)

    def _path(self, digest):
        require(type(digest) is str and DIGEST.fullmatch(digest) is not None)
        return reject_path_aliases(self.cache_path / (digest + ".json"))

    def read_object(self, entry):
        target = self._path(entry["sha256"])
        if private_file_identity(target, missing_ok=True) is None:
            return None
        with target.open("rb") as handle:
            raw = handle.read(OBJECT_LIMIT + 1)
        require(len(raw) <= OBJECT_LIMIT)
        return raw

    def promote(self, raw, entry, retained):
        # Call only after authority save. Links/aliases are rejected before each
        # boundary, and a failed object write never rolls back a withdrawal.
        target = self._path(entry["sha256"])
        target.parent.mkdir(parents=True, exist_ok=True)
        reject_path_aliases(target.parent)
        temporary = reject_path_aliases(target.parent / (".parameter-" + uuid4().hex))
        try:
            with temporary.open("xb") as handle:
                apply_private_file_security(temporary)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            reject_path_aliases(target)
            os.replace(temporary, target)
            apply_private_file_security(target)
            sync_file(target)
            sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        # At most one object per authenticated mapping (128 * 16 KiB).
        for old in target.parent.glob("*.json"):
            if DIGEST.fullmatch(old.stem) and old.stem not in retained:
                reject_path_aliases(old).unlink()
