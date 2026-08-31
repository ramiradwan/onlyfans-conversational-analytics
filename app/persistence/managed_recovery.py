"""Bounded cleanup for Product-managed recovery files."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.retention import MANAGED_RECOVERY_MAX_DAYS

_MIGRATION_BACKUP_TIME = re.compile(r"\.(?P<stamp>\d{8}T\d{12}Z)-[0-9a-f]{8}\.bak$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def managed_recovery_created_at(path: str | Path) -> datetime | None:
    """Return the immutable creation time encoded by a managed recovery cohort."""
    target = Path(path)
    manifest = target.with_name(target.name + ".manifest.json")
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            return datetime.fromisoformat(str(payload["created_at"])).astimezone(timezone.utc)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    match = _MIGRATION_BACKUP_TIME.search(target.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def managed_recovery_is_current(
    path: str | Path,
    *,
    now: datetime,
    max_age_days: int = MANAGED_RECOVERY_MAX_DAYS,
) -> bool:
    """Return whether a managed recovery cohort remains inside its bounded horizon."""
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    created_at = managed_recovery_created_at(path)
    if created_at is None:
        return False
    return created_at >= _aware(now) - timedelta(days=max_age_days)


def prune_managed_recovery_files(
    directory: str | Path,
    *,
    now: datetime,
    max_age_days: int = MANAGED_RECOVERY_MAX_DAYS,
) -> list[Path]:
    """Delete complete managed backup cohorts older than the bounded horizon."""
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    root = Path(directory)
    if not root.exists():
        return []
    cutoff = _aware(now) - timedelta(days=max_age_days)
    removed: list[Path] = []
    for path in root.iterdir():
        if not path.is_file() or path.name.endswith((".manifest.json", ".key.dpapi")):
            continue
        created_at = managed_recovery_created_at(path)
        if created_at is None or created_at >= cutoff:
            continue
        for member in (
            path,
            path.with_name(path.name + ".manifest.json"),
            path.with_name(path.name + ".key.dpapi"),
        ):
            if member.exists():
                member.unlink()
                removed.append(member)
    return removed
