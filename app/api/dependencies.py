"""Replaceable account-session dependencies for the analytics HTTP endpoints.

Account authority for analytics reads comes from the same runtime policy used
by the other HTTP surfaces.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from app.api.security import get_runtime_policy
from app.security.runtime_policy import (
    RuntimeAuthorizationDenied,
    RuntimePolicy,
    authorized_account,
)


def _detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def get_authenticated_account_session(
    policy: RuntimePolicy = Depends(get_runtime_policy),
) -> RuntimePolicy:
    """Authenticate the analytics HTTP surface with one runtime policy."""

    return policy


def account_bound_to_session(
    policy: RuntimePolicy,
    requested_account_id: str | None,
) -> str:
    """Resolve one account partition from a complete runtime policy."""

    try:
        return authorized_account(policy, requested_account_id)
    except RuntimeAuthorizationDenied as error:
        raise HTTPException(
            status_code=403,
            detail=_detail(
                "account_binding_mismatch",
                "The authenticated session cannot access that account.",
            ),
        ) from error
