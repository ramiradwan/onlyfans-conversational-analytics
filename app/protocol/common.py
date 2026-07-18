"""Shared, closed protocol v1 value objects.

These models deliberately avoid untyped dictionaries so every nested object is
part of the executable contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PROTOCOL_VERSION = "1"

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonEmptyString = Annotated[str, Field(min_length=1)]
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    """Base for every protocol object; coercion and extension fields are forbidden."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RawChat(StrictModel):
    chat_id: NonEmptyString
    platform_user_id: NonEmptyString
    display_name: str | None
    updated_at: datetime


class RawMessage(StrictModel):
    message_id: NonEmptyString
    chat_id: NonEmptyString
    sender_platform_user_id: NonEmptyString
    text: str
    sent_at: datetime
    direction: Literal["inbound", "outbound"]


class ChatUpsertChange(StrictModel):
    type: Literal["chat.upsert"]
    chat: RawChat


class ChatDeleteChange(StrictModel):
    type: Literal["chat.delete"]
    chat_id: NonEmptyString


class MessageUpsertChange(StrictModel):
    type: Literal["message.upsert"]
    message: RawMessage


class MessageDeleteChange(StrictModel):
    type: Literal["message.delete"]
    message_id: NonEmptyString
    chat_id: NonEmptyString


RawIngestChange = Annotated[
    Union[
        ChatUpsertChange,
        ChatDeleteChange,
        MessageUpsertChange,
        MessageDeleteChange,
    ],
    Field(discriminator="type"),
]


class MessageView(StrictModel):
    message_id: NonEmptyString
    text: str
    sent_at: datetime
    direction: Literal["inbound", "outbound"]
    sentiment: Literal["positive", "neutral", "negative", "unknown"]


class ConversationView(StrictModel):
    conversation_id: NonEmptyString
    platform_user_id: NonEmptyString
    display_name: str | None
    unread_count: NonNegativeInt
    last_message_at: datetime | None
    messages: list[MessageView]


class AnalyticsView(StrictModel):
    total_conversations: NonNegativeInt
    total_messages: NonNegativeInt
    inbound_messages: NonNegativeInt
    outbound_messages: NonNegativeInt


class ConversationUpsertChange(StrictModel):
    type: Literal["conversation.upsert"]
    conversation: ConversationView


class ConversationDeleteChange(StrictModel):
    type: Literal["conversation.delete"]
    conversation_id: NonEmptyString


class AnalyticsReplaceChange(StrictModel):
    type: Literal["analytics.replace"]
    analytics: AnalyticsView


StateChange = Annotated[
    Union[
        ConversationUpsertChange,
        ConversationDeleteChange,
        AnalyticsReplaceChange,
    ],
    Field(discriminator="type"),
]


class HealthSummary(StrictModel):
    status: Literal["healthy", "degraded"]
    detail: str | None


class LastPresenceObservation(StrictModel):
    observation_id: NonNegativeInt
    observed_at: datetime


class CapabilityStatus(StrictModel):
    capability: Literal["capture.chats", "capture.messages", "capture.presence", "command.message.send"]
    status: Literal["active", "degraded", "unsupported"]
    detail: str | None


class MessageSendAction(StrictModel):
    type: Literal["message.send"]
    conversation_id: NonEmptyString
    text: NonEmptyString
    media_url: str | None


CommandAction = Annotated[MessageSendAction, Field(discriminator="type")]


class CommandOutput(StrictModel):
    external_message_id: str | None


class CommandError(StrictModel):
    code: Literal["rejected", "deadline_exceeded", "platform_error", "execution_error"]
    detail: NonEmptyString
    retryable: bool


class CaptureRule(StrictModel):
    resource: Literal["chats", "messages", "presence"]
    url_pattern: NonEmptyString
    enabled: bool


class CapturePolicy(StrictModel):
    observation_interval_seconds: Annotated[int, Field(ge=5, le=3600)]
    rules: Annotated[list[CaptureRule], Field(min_length=1)]


class CommandPolicy(StrictModel):
    allowed_actions: list[Literal["message.send"]]
    max_text_length: PositiveInt
    require_idempotency: bool


class EnvelopeFields(StrictModel):
    """Documentation model for the fields repeated by each discriminated message."""

    type: NonEmptyString
    protocol_version: Literal["1"]
    message_id: UUID
    correlation_id: UUID | None = None
