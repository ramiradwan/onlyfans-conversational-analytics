from __future__ import annotations

import pytest

from app.persistence import sqlite_api as sqlite3
from app.persistence.backup import SQLiteBackupError
from app.persistence.retention_restore import _current_authority


class UnreadableAuthorityDatabase:
    def read(self):
        raise sqlite3.DatabaseError("synthetic authority read failure")


def test_current_restore_authority_read_failure_is_not_treated_as_empty() -> None:
    with pytest.raises(
        SQLiteBackupError,
        match="current_retention_authority_unavailable",
    ):
        _current_authority(UnreadableAuthorityDatabase())  # type: ignore[arg-type]
