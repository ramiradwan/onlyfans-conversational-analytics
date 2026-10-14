"""Closed companion wrapper; account authority never enters the resolver DTO."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from app.protocol.common import StrictModel

METHOD = "agent.signing-rule.resolve"
CAPABILITY = "signing.parameters.v1"
Sequence = Annotated[int, Field(ge=0, le=2**53 - 1)]


class ParameterRequest(StrictModel):
    schema: Literal["signing-parameter-request/v1"]
    engine: Literal["onlyfans-sha1-checksum/v1"]
    parameter_schema: Literal["local-packaged-signing-rule/v1"]
    source_revision: Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,128}$")]
    catalog_sequence: Sequence
    keyset_sequence: Sequence
    object_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None


class SigningParameterResolveRequest(StrictModel):
    operation: Literal["agent.signing-rule.resolve"]
    protocol_version: Literal["2"]
    auth_ticket: Annotated[str, Field(min_length=1, max_length=4096)]
    agent_installation_id: UUID
    creator_account_id: Annotated[str, Field(min_length=1, max_length=256)]
    request: ParameterRequest
