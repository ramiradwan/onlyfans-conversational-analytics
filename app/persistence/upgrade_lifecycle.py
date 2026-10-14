"""Read-only schema admission before any migrating application constructor.

The composition owner holds the process-lifetime root lease. Catalog paths are
inputs, so this persistence owner never imports the analytics application.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.persistence.database import open_encrypted_sqlite
from app.persistence.migrations import MigrationRunner, SchemaInspection, SchemaCompatibilityError
from app.persistence.private_files import private_file_identity, reject_path_aliases
from app.security.local_data_key import database_key


@dataclass(frozen=True, slots=True)
class UpgradeStore:
    filename: str
    key_scope: str
    catalog: Path


def inspect_existing_stores(root: Path, stores: tuple[UpgradeStore, ...]) -> dict[str, SchemaInspection]:
    """Open all retained stores with existing keys, no SQL or ACL repair.

    Immutable opening is limited to a closed database without a WAL, under the
    root lease. It would hide committed WAL rows. WAL inspection uses ordinary
    read-only opening and requires the existing WAL index; missing-index
    recovery is explicit rather than creating files during admission. SQLite
    may update its existing scratch WAL index, but cannot mutate authority.
    """
    result: dict[str, SchemaInspection] = {}
    for store in stores:
        path = reject_path_aliases(root / store.filename)
        if not path.exists():
            continue
        identity = private_file_identity(path, repair_permissions=False)
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        for sidecar in (wal, shm):
            reject_path_aliases(sidecar)
            if sidecar.exists():
                private_file_identity(sidecar, repair_permissions=False)
        if wal.exists() and not shm.is_file():
            raise SchemaCompatibilityError("retained WAL requires explicit index recovery")
        key = database_key(path, store.key_scope, create_if_missing=False)
        with closing(open_encrypted_sqlite(path, key, read_only=True, immutable=not wal.exists())) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            result[store.filename] = MigrationRunner.inspect(connection, migrations_dir=store.catalog)
        if private_file_identity(path, repair_permissions=False) != identity:
            raise SchemaCompatibilityError("retained database changed during inspection")
    return result
