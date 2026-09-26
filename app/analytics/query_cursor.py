"""Authenticated, bounded continuation tokens for analytics question pages."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Literal

from pydantic import ValidationError

from app.analytics.errors import InvalidAnalyticsRequest
from app.analytics.query_contracts import Instant, PagePosition, QuestionRecord
from app.models.analytics import Sha256Digest


class InvalidQuestionCursor(InvalidAnalyticsRequest):
    def __init__(self) -> None:
        super().__init__(
            "analytics_question_cursor_invalid",
            "This page is not valid for the selected question. Run the question again.",
        )


class StaleQuestionCursor(InvalidAnalyticsRequest):
    def __init__(self) -> None:
        super().__init__(
            "analytics_question_cursor_stale",
            "The results changed or expired. Run the question again.",
        )


class QuestionCursor(QuestionRecord):
    schema_version: Literal["analytics-question-cursor.v1"] = (
        "analytics-question-cursor.v1"
    )
    request_digest: Sha256Digest
    snapshot_digest: Sha256Digest
    definition_digest: Sha256Digest
    cutoff: Instant
    expires_at: Instant
    after: PagePosition


def digest(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return (
        "sha256:"
        + hashlib.sha256(b"ofca:analytics-question:v1\0" + encoded).hexdigest()
    )


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    data = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if not value or _encode(data) != value:
        raise InvalidQuestionCursor()
    return data


class QuestionCursorCodec:
    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secrets.token_bytes(32) if secret is None else secret
        if not isinstance(self._secret, bytes) or len(self._secret) < 32:
            raise ValueError("question_cursor_secret_invalid")

    def encode(self, cursor: QuestionCursor) -> str:
        cursor = QuestionCursor.model_validate(cursor)
        payload = cursor.model_dump_json().encode("utf-8")
        signature = hmac.digest(
            self._secret, b"ofca:question-cursor:v1\0" + payload, "sha256"
        )
        token = _encode(payload) + "." + _encode(signature)
        if len(token) > 4096:
            raise InvalidQuestionCursor()
        return token

    def decode(self, token: str) -> QuestionCursor:
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 4096:
                raise InvalidQuestionCursor()
            payload_part, signature_part = token.split(".")
            payload, signature = _decode(payload_part), _decode(signature_part)
            expected = hmac.digest(
                self._secret, b"ofca:question-cursor:v1\0" + payload, "sha256"
            )
            if not hmac.compare_digest(signature, expected):
                raise InvalidQuestionCursor()
            return QuestionCursor.model_validate_json(payload)
        except (ValueError, ValidationError, UnicodeError):
            raise InvalidQuestionCursor() from None
