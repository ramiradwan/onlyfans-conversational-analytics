"""Stable identities for deterministic analytics adapters and formulas."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_config_digest(*, name: str, revision: str, config: Any) -> str:
    """Hash an adapter/formula configuration using canonical JSON."""

    payload = {"name": name, "revision": revision, "config": config}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
