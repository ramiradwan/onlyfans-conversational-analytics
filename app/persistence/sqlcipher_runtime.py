"""Runtime identity and fail-closed encryption probes for the SQLCipher driver."""

from __future__ import annotations

import importlib
import sqlite3 as standard_sqlite
import tempfile
from pathlib import Path
from typing import Any

from app.persistence import sqlite_api


MINIMUM_SQLITE = (3, 51, 3)
MINIMUM_SQLCIPHER = (4, 14, 0)


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    numeric: list[int] = []
    for part in parts:
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        numeric.append(int(digits))
        if len(numeric) == 3:
            break
    if len(numeric) < 2:
        raise ValueError(f"unrecognised version: {value!r}")
    return tuple((numeric + [0, 0, 0])[:3])  # type: ignore[return-value]


def runtime_identity() -> dict[str, Any]:
    """Return the native module and version facts from the active DB-API driver."""

    module = importlib.import_module("sqlcipher3._sqlite3")
    with sqlite_api.connect(":memory:") as connection:
        cipher_version = sqlite_api.require_cipher(connection)
    sqlite_version = str(sqlite_api.sqlite_version)
    return {
        "dbapi_module": "sqlcipher3.dbapi2",
        "extension_path": str(Path(str(module.__file__)).resolve()),
        "sqlite_version": sqlite_version,
        "sqlcipher_version": cipher_version,
        "minimum_sqlite": ".".join(map(str, MINIMUM_SQLITE)),
        "minimum_sqlcipher": ".".join(map(str, MINIMUM_SQLCIPHER)),
        "version_qualified": (
            _version_tuple(sqlite_version) >= MINIMUM_SQLITE
            and _version_tuple(cipher_version) >= MINIMUM_SQLCIPHER
        ),
    }


def encryption_fail_closed_probe() -> dict[str, bool]:
    """Prove an encrypted test file remains unreadable through stdlib SQLite."""

    with tempfile.TemporaryDirectory(prefix="ofca-sqlcipher-probe-") as temporary:
        database = Path(temporary) / "encrypted.sqlite3"
        key = "a1" * 32
        connection = sqlite_api.connect(database)
        try:
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
            sqlite_api.require_cipher(connection)
            connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO probe(value) VALUES ('encrypted')")
            connection.commit()
        finally:
            connection.close()
        connection = sqlite_api.connect(database)
        try:
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
            readback = connection.execute("SELECT value FROM probe").fetchone()[0]
            integrity = connection.execute("PRAGMA cipher_integrity_check").fetchone()
        finally:
            connection.close()
        wrong_key_rejected = False
        connection = sqlite_api.connect(database)
        try:
            connection.execute("PRAGMA key = \"x'00'\"")
            connection.execute("SELECT value FROM probe").fetchone()
        except sqlite_api.DatabaseError:
            wrong_key_rejected = True
        finally:
            connection.close()
        plaintext_rejected = False
        connection = standard_sqlite.connect(database)
        try:
            connection.execute("SELECT value FROM probe").fetchone()
        except standard_sqlite.DatabaseError:
            plaintext_rejected = True
        finally:
            connection.close()
    integrity_result = "unsupported" if integrity is None else str(integrity[0]).lower()
    return {
        # SQLCipher builds can omit this optional diagnostic pragma. The
        # report distinguishes that case from a supported failing check.
        "cipher_integrity_check": integrity_result,
        "encrypted_readback": readback == "encrypted",
        "stdlib_sqlite_rejected": plaintext_rejected,
        "wrong_key_rejected": wrong_key_rejected,
    }


def qualification_report() -> dict[str, Any]:
    """Return a JSON-safe runtime qualification record without changing state."""

    identity = runtime_identity()
    encryption = encryption_fail_closed_probe()
    identity["encryption"] = encryption
    identity["qualified"] = bool(
        identity["version_qualified"]
        and encryption["cipher_integrity_check"] in {"ok", "unsupported"}
        and encryption["encrypted_readback"]
        and encryption["stdlib_sqlite_rejected"]
        and encryption["wrong_key_rejected"]
    )
    return identity
