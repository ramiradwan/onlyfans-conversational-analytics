"""Replaceable account-session dependencies for the analytics HTTP endpoints.

Account authority for analytics reads comes from the same signer-v2
same-origin bridge session cookie that ``app.api.endpoints.history`` already
authenticates against (see ``app.api.security.get_auth_context``). There is
no analytics-specific ticket, header, or query-string account seam.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from app.api.security import AuthContext, get_auth_context
from app.models.auth import AuthenticatedAccountSession


def _detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def get_authenticated_account_session(
    context: AuthContext = Depends(get_auth_context),
) -> AuthenticatedAccountSession:
    """Authenticate the analytics HTTP surface from the bridge session cookie."""

    return AuthenticatedAccountSession(
        principal_id=context.principal_id,
        creator_account_id=context.creator_account_id,
    )


def account_bound_to_session(
    session: AuthenticatedAccountSession,
    requested_account_id: str | None,
) -> str:
    """Use session authority and reject every mismatched request partition."""

    if (
        requested_account_id is not None
        and requested_account_id != session.creator_account_id
    ):
        raise HTTPException(
            status_code=403,
            detail=_detail(
                "account_binding_mismatch",
                "The authenticated session cannot access that account.",
            ),
        )
    return session.creator_account_id
