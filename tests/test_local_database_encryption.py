from __future__ import annotations

import sqlite3 as plaintext_sqlite
from pathlib import Path

import pytest
from sqlcipher3 import dbapi2 as cipher_sqlite

from app.persistence.database import (
    AuthSQLite,
    CanonicalSQLite,
    SQLiteConfigurationError,
)
from app.security.local_data_key import database_key


def _write_secret(path: Path) -> CanonicalSQLite:
    database = CanonicalSQLite(path)
    with database.transaction() as connection:
        connection.execute("CREATE TABLE private_values (value TEXT NOT NULL)")
        connection.execute("INSERT INTO private_values VALUES ('creator-secret')")
    return database


def test_database_file_has_no_plaintext_header_or_value(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    database = _write_secret(path)

    raw = path.read_bytes()
    assert not raw.startswith(b"SQLite format 3\x00")
    assert b"creator-secret" not in raw
    with database.read() as connection:
        assert connection.execute("SELECT value FROM private_values").fetchone()[0] == (
            "creator-secret"
        )


def test_standard_sqlite_and_wrong_scope_cannot_open_store(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    _write_secret(path)

    with pytest.raises(plaintext_sqlite.DatabaseError):
        with plaintext_sqlite.connect(path) as connection:
            connection.execute("SELECT value FROM private_values").fetchall()

    wrong_scope = AuthSQLite(path)
    with pytest.raises(cipher_sqlite.DatabaseError):
        with wrong_scope.read() as connection:
            connection.execute("SELECT value FROM private_values").fetchall()


def test_explicit_test_key_is_refused_outside_test_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SQLiteConfigurationError):
        CanonicalSQLite(tmp_path / "canonical.sqlite3")


def test_database_scopes_derive_distinct_keys(tmp_path: Path) -> None:
    assert database_key(tmp_path / "auth.sqlite3", "auth") != database_key(
        tmp_path / "canonical.sqlite3", "canonical"
    )
