"""Opaque HMAC-authenticated cursors for stable projection message paging."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


class InvalidMessageCursor(ValueError):
    pass


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise InvalidMessageCursor("cursor encoding is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise InvalidMessageCursor("cursor encoding is invalid") from error
    if _encode_base64(decoded) != value:
        raise InvalidMessageCursor("cursor encoding is not canonical")
    return decoded


@dataclass(frozen=True, slots=True)
class MessageCursor:
    account_id: str
    conversation_id: str
    projection_generation: str
    projection_revision: int
    sent_at: str
    message_id: str
    version: int = 1


class MessageCursorCodec:
    def __init__(self, secret: str) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("cursor signing secret must contain at least 32 bytes")
        self._secret = secret.encode("utf-8")

    def encode(self, cursor: MessageCursor) -> str:
        payload = json.dumps(
            asdict(cursor), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_encode_base64(payload)}.{_encode_base64(signature)}"

    def decode(self, token: str) -> MessageCursor:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
        except ValueError as error:
            raise InvalidMessageCursor("cursor must contain a payload and signature") from error
        payload = _decode_base64(encoded_payload)
        signature = _decode_base64(encoded_signature)
        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise InvalidMessageCursor("cursor signature is invalid")
        try:
            document: Any = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidMessageCursor("cursor payload is invalid") from error
        expected_keys = {
            "account_id",
            "conversation_id",
            "projection_generation",
            "projection_revision",
            "sent_at",
            "message_id",
            "version",
        }
        if not isinstance(document, dict) or set(document) != expected_keys:
            raise InvalidMessageCursor("cursor payload shape is invalid")
        if document["version"] != 1:
            raise InvalidMessageCursor("cursor version is unsupported")
        for field in (
            "account_id",
            "conversation_id",
            "projection_generation",
            "sent_at",
            "message_id",
        ):
            if not isinstance(document[field], str) or not document[field]:
                raise InvalidMessageCursor(f"cursor {field} is invalid")
        if (
            isinstance(document["projection_revision"], bool)
            or not isinstance(document["projection_revision"], int)
            or document["projection_revision"] < 0
        ):
            raise InvalidMessageCursor("cursor projection_revision is invalid")
        try:
            sent_at = datetime.fromisoformat(document["sent_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise InvalidMessageCursor("cursor sent_at is invalid") from error
        if sent_at.tzinfo is None or sent_at.utcoffset() is None:
            raise InvalidMessageCursor("cursor sent_at must include a UTC offset")
        return MessageCursor(**document)
