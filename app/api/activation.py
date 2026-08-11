"""HTTP adapter for the runtime activation gate."""

from __future__ import annotations

from fastapi import HTTPException

from app.security.activation_gate import runtime_is_activated


def require_activated_runtime() -> None:
    """Refuse a runtime route while the activation conditions are unmet."""

    if not runtime_is_activated():
        raise HTTPException(
            status_code=503, detail="The local runtime is not activated"
        )
