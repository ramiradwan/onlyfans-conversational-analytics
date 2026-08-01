"""One-time production configuration for the local launcher."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from app.core.runtime_paths import runtime_configuration_file, runtime_data_directory


_RESERVED_PREFIX = "replace-with-"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXTENSION_ID_PATTERN = re.compile(r"[a-p]{32}")
_SECRET_BYTES = 32


class FirstRunConflictError(RuntimeError):
    """Existing runtime configuration has different provisioned bindings."""


@dataclass(frozen=True, slots=True)
class VerifiedGrantBindings:
    """Bindings emitted by the grant verifier after exact grant validation."""

    principal_id: str
    creator_account_id: str
    platform_creator_id: str
    bridge_role: Literal["creator", "operator"]
    grant_bundle_sha256: str

    def __post_init__(self) -> None:
        bindings = (
            self.principal_id,
            self.creator_account_id,
            self.platform_creator_id,
        )
        if not all(bindings) or any(
            value.startswith(_RESERVED_PREFIX) for value in bindings
        ):
            raise ValueError("verified grant bindings must be exact non-placeholder values")
        if _SHA256_PATTERN.fullmatch(self.grant_bundle_sha256) is None:
            raise ValueError("verified grant bundle digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class FirstRunResult:
    data_directory: Path
    configuration_file: Path
    created: bool


def initialize_production_configuration(
    *,
    bindings: VerifiedGrantBindings,
    extension_id: str,
    data_directory: str | Path | None = None,
) -> FirstRunResult:
    """Create production configuration once without returning either secret."""

    if _EXTENSION_ID_PATTERN.fullmatch(extension_id) is None:
        raise ValueError("extension_id must be an exact Chrome extension ID")

    target_directory = runtime_data_directory(data_directory)
    target = runtime_configuration_file(target_directory)
    expected = _nonsecret_configuration(bindings, extension_id, target_directory)
    if target.exists():
        _require_matching_existing_configuration(target, expected)
        return FirstRunResult(target_directory, target, False)

    target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {
        **expected,
        "LOCAL_SESSION_BOOTSTRAP_TOKEN": secrets.token_urlsafe(_SECRET_BYTES),
        "SECURITY_SIGNING_SECRET": secrets.token_urlsafe(_SECRET_BYTES),
    }
    content = "".join(
        f"{key}={json.dumps(value, ensure_ascii=True)}\n"
        for key, value in values.items()
    ).encode("utf-8")

    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _require_matching_existing_configuration(target, expected)
        return FirstRunResult(target_directory, target, False)

    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    return FirstRunResult(target_directory, target, True)


def _nonsecret_configuration(
    bindings: VerifiedGrantBindings,
    extension_id: str,
    data_directory: Path,
) -> dict[str, str]:
    return {
        "ENVIRONMENT": "production",
        "WEBSOCKET_AUTH_MODE": "local_session",
        "WEBSOCKET_BIND_HOST": "127.0.0.1",
        "IDENTITY_BINDING_SOURCE": "verified_grants",
        "VERIFIED_GRANT_BUNDLE_SHA256": bindings.grant_bundle_sha256,
        "LOCAL_PRINCIPAL_ID": bindings.principal_id,
        "LOCAL_CREATOR_ACCOUNT_ID": bindings.creator_account_id,
        "LOCAL_PLATFORM_CREATOR_ID": bindings.platform_creator_id,
        "LOCAL_BRIDGE_ROLE": bindings.bridge_role,
        "CANONICAL_PERSISTENCE_BACKEND": "sqlite",
        "AUTH_DATABASE_PATH": str(data_directory / "auth.sqlite3"),
        "CANONICAL_DATABASE_PATH": str(data_directory / "canonical.sqlite3"),
        "PROJECTION_DATABASE_PATH": str(data_directory / "projections.sqlite3"),
        "ANALYTICS_PROJECTION_DATABASE_PATH": str(
            data_directory / "analytics-projections.sqlite3"
        ),
        "EXTENSION_ID": extension_id,
        "BROADCAST_URL": "memory://",
    }


def _require_matching_existing_configuration(
    target: Path,
    expected: dict[str, str],
) -> None:
    existing = dotenv_values(target, interpolate=False)
    mismatches = [key for key, value in expected.items() if existing.get(key) != value]
    bootstrap = existing.get("LOCAL_SESSION_BOOTSTRAP_TOKEN") or ""
    signing = existing.get("SECURITY_SIGNING_SECRET") or ""
    invalid_secrets = (
        len(bootstrap) < 32
        or bootstrap.startswith(_RESERVED_PREFIX)
        or len(signing) < 32
        or signing.startswith(_RESERVED_PREFIX)
    )
    if mismatches or invalid_secrets:
        raise FirstRunConflictError(
            "existing runtime configuration does not match verified first-run inputs"
        )
