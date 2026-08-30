"""Strict HTTP contracts for Full-mode encrypted browser persistence."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.protocol.common import NonEmptyString, StrictModel


class ExtensionStorageUnsealRequest(StrictModel):
    storage_bootstrap: NonEmptyString


class ExtensionStorageUnlockResponse(StrictModel):
    schema_version: Literal["ofca-extension-storage-unlock/v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    creator_account_id: NonEmptyString
    credential_kind: Literal["pairing", "reconnect"]
    auth_ticket: NonEmptyString
    storage_key_base64: NonEmptyString


class ExtensionStorageRotateRequest(StrictModel):
    protocol_version: Literal["2"]
    creator_account_id: NonEmptyString
    agent_installation_id: Annotated[UUID, Field(strict=False)]
    reconnect_auth_ticket: NonEmptyString
    storage_bootstrap: NonEmptyString


class ExtensionStorageRotateResponse(StrictModel):
    schema_version: Literal["ofca-extension-storage-rotation/v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    storage_bootstrap: NonEmptyString
