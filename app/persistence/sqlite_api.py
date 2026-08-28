"""SQLCipher DB-API facade used by every application SQLite consumer."""

from __future__ import annotations

from sqlcipher3 import dbapi2 as _dbapi


Binary = _dbapi.Binary
Connection = _dbapi.Connection
Cursor = _dbapi.Cursor
DataError = _dbapi.DataError
DatabaseError = _dbapi.DatabaseError
Error = _dbapi.Error
IntegrityError = _dbapi.IntegrityError
InterfaceError = _dbapi.InterfaceError
InternalError = _dbapi.InternalError
NotSupportedError = _dbapi.NotSupportedError
OperationalError = _dbapi.OperationalError
ProgrammingError = _dbapi.ProgrammingError
Row = _dbapi.Row
Warning = _dbapi.Warning
connect = _dbapi.connect
sqlite_version = _dbapi.sqlite_version


def require_cipher(connection: Connection) -> str:
    """Return the SQLCipher version or fail closed for an unencrypted driver."""

    row = connection.execute("PRAGMA cipher_version").fetchone()
    version = "" if row is None or row[0] is None else str(row[0]).strip()
    if not version:
        raise OperationalError("SQLCipher support is unavailable")
    return version
