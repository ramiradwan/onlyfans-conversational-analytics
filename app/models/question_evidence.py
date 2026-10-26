"""Closed source references shared by questions and retained findings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, model_validator

Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
AccountRef = Annotated[str, Field(pattern=r"^a1:[0-9a-f]{64}$")]
ConversationRef = Annotated[str, Field(pattern=r"^c1:[0-9a-f]{64}$")]
MessageRef = Annotated[str, Field(pattern=r"^m1:[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]


def utc_instant(value: object) -> datetime:
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("question_timestamp_invalid") from None
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("question_timestamp_requires_offset")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("question_timestamp_invalid") from None


Instant = Annotated[datetime, BeforeValidator(utc_instant)]


class QuestionRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class SourceSpan(QuestionRecord):
    start: Count
    end: Count

    @model_validator(mode="after")
    def nonempty(self) -> SourceSpan:
        if self.start >= self.end:
            raise ValueError("question_span_invalid")
        return self


class QuestionEvidence(QuestionRecord):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    message_ref: MessageRef
    source_revision: Count
    source_version_digest: Sha256Digest
    sent_at: Instant
    span: SourceSpan | None = None
