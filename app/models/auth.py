"""Replaceable HTTP-session shape bound to exactly one account partition."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AuthenticatedAccountSession(BaseModel):
    """Session-derived account authority for the analytics HTTP surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    creator_account_id: str
