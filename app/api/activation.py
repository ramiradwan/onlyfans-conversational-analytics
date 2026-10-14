"""Local installation trust gate; commercial licensing is checked separately."""

from __future__ import annotations

from fastapi import HTTPException

from app.security.activation_gate import runtime_is_activated


def require_activated_runtime() -> None:
    """Require the configured installation, independently of its Full license."""

    if not runtime_is_activated():
        raise HTTPException(
            status_code=503, detail="The local runtime is not activated"
        )
