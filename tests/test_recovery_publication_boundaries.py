from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.private_files import sync_directory


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def test_directory_sync_does_not_apply_managed_recovery_policy(tmp_path: Path) -> None:
    backup = tmp_path / "old.backup"
    manifest = backup.with_name(backup.name + ".manifest.json")
    key = backup.with_name(backup.name + ".key.dpapi")

    backup.write_text("encrypted", encoding="utf-8")
    manifest.write_text(
        json.dumps({"created_at": (NOW - timedelta(days=31)).isoformat()}),
        encoding="utf-8",
    )
    key.write_text("wrapped", encoding="utf-8")

    sync_directory(tmp_path)

    assert backup.exists()
    assert manifest.exists()
    assert key.exists()
