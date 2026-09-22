"""Current-user protected handoff recovery for the same provisioning process."""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    reject_path_aliases,
    sync_directory,
    sync_file,
)
from app.security.local_data_key import LocalDataKeyError, protect_local_secret, unprotect_local_secret

FILENAME = ".provisioning-launcher.dpapi"
PURPOSE = "provisioning-launcher-handoff-v1"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def save_launcher_handoff(directory: Path, *, token: str, pid: int) -> None:
    if not _TOKEN.fullmatch(token) or type(pid) is not int or pid <= 0:
        raise ValueError("invalid provisioning launcher handoff")
    target = reject_path_aliases(directory / FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    plaintext = json.dumps({"token": token, "pid": pid}, separators=(",", ":")).encode("ascii")
    payload = protect_local_secret(plaintext, purpose=PURPOSE)
    temporary = target.with_name(f"{FILENAME}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        apply_private_file_security(temporary)
        sync_file(temporary)
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def load_launcher_handoff(directory: Path, *, pid: int) -> str:
    target = reject_path_aliases(directory / FILENAME)
    if target.stat().st_size > 8192:
        raise ValueError("invalid provisioning launcher handoff")
    payload = unprotect_local_secret(target.read_bytes(), purpose=PURPOSE)
    record = json.loads(payload)
    if (
        not isinstance(record, dict) or set(record) != {"token", "pid"}
        or type(record["pid"]) is not int or record["pid"] != pid
        or not isinstance(record["token"], str) or not _TOKEN.fullmatch(record["token"])
    ):
        raise ValueError("invalid provisioning launcher handoff")
    return record["token"]


def remove_launcher_handoff(directory: Path, *, pid: int) -> None:
    """Retire only this process's ephemeral credential, preserving another owner."""
    try:
        load_launcher_handoff(directory, pid=pid)
    except (OSError, ValueError, PrivateFileSecurityError, LocalDataKeyError):
        return
    (directory / FILENAME).unlink(missing_ok=True)
