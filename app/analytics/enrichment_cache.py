"""Versioned, bounded reuse records for independent message analyzers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app.analytics.cancellation import check_cancelled
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.provenance import stable_config_digest
from app.models.analytics import (
    AccountRef, ConversationRef, MessageRef, Sha256Digest, MessageAnalysisInput,
    SentimentResult, TopicEntityResult, EngagementResult,
)

MAX_CACHE_ENTRIES = 30_000
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 65_536
CACHE_BATCH_SIZE = 64
RESULT_TYPES = {"sentiment": SentimentResult, "topic_entities": TopicEntityResult,
                "engagement": EngagementResult}
Slot = Literal["sentiment", "topic_entities", "engagement"]


class CacheRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AnalyzerCachePolicy(CacheRecord):
    """Opt in only when these inputs fully determine an analyzer's result."""

    model_digest: Sha256Digest | None = None
    taxonomy_revision: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    context_revision: Annotated[StrictStr, Field(min_length=1, max_length=128)] = "message.v1"
    preceding_messages: Annotated[StrictInt, Field(ge=0, le=32)] = 0
    following_messages: Annotated[StrictInt, Field(ge=0, le=32)] = 0


class EnrichmentKey(CacheRecord):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    message_ref: MessageRef
    slot: Slot
    input_digest: Sha256Digest
    adapter_digest: Sha256Digest
    context_digest: Sha256Digest
    expires_at: AwareDatetime

    @property
    def digest(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


def fingerprint(value: object) -> str:
    return stable_config_digest(name="enrichment_reuse", revision="v1", config=value)


def input_document(message: MessageAnalysisInput) -> dict:
    document = message.model_dump(mode="json")
    document["sent_at"] = message.sent_at.astimezone(timezone.utc).isoformat()
    return document


def enrichment_key(message, slot, descriptor, policy, context) -> EnrichmentKey:
    dependencies = (message, *context)
    return EnrichmentKey(
        account_ref=account_ref(message.creator_account_id),
        conversation_ref=conversation_ref(message.creator_account_id, message.conversation_id),
        message_ref=message_ref(message.creator_account_id, message.conversation_id, message.message_id),
        slot=slot, input_digest=fingerprint(input_document(message)),
        adapter_digest=fingerprint({"descriptor": descriptor.model_dump(mode="json"),
                                    "policy": policy.model_dump(mode="json")}),
        context_digest=fingerprint([input_document(item) for item in context]),
        expires_at=min(item.sent_at for item in dependencies).astimezone(timezone.utc)
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
    )


class CachedEnrichment(CacheRecord):
    key: EnrichmentKey
    result_json: Annotated[StrictStr, Field(max_length=MAX_ENTRY_BYTES)] = Field(repr=False)

    def result(self):
        return RESULT_TYPES[self.key.slot].model_validate_json(self.result_json)


class EnrichmentReuse:
    """Keep only one lookup batch and a bounded set of staging records."""

    def __init__(self, store, account_id: str, clock: Callable[[], datetime], cancellation=None):
        self.store, self.account_id = store, account_id
        self.clock, self.cancellation = clock, cancellation
        self.entries: dict[str, bytes] = {}
        self._batch: dict[str, bytes] = {}
        self.bytes_used = 0

    def prefetch(self, keys: list[EnrichmentKey]) -> None:
        check_cancelled(self.cancellation)
        self._batch = self.store.load_enrichment_entries(
            self.account_id, [key.digest for key in keys],
            now=self.clock(), cancellation_check=self.cancellation,
        ) if keys else {}

    def get(self, key: EnrichmentKey):
        check_cancelled(self.cancellation)
        data = self._batch.get(key.digest)
        if data is None or len(data) > MAX_ENTRY_BYTES or key.expires_at <= self.clock():
            return None
        try:
            entry = CachedEnrichment.model_validate_json(data)
            return entry.result() if entry.key == key else None
        except (ValueError, TypeError):
            return None

    def retain(self, key: EnrichmentKey, result) -> None:
        check_cancelled(self.cancellation)
        if key.expires_at <= self.clock() or len(self.entries) >= MAX_CACHE_ENTRIES:
            return
        result_json = result.model_dump_json()
        if len(result_json.encode("utf-8")) > MAX_ENTRY_BYTES:
            return
        entry = CachedEnrichment(key=key, result_json=result_json)
        data = entry.model_dump_json().encode("utf-8")
        if len(data) > MAX_ENTRY_BYTES or self.bytes_used + len(data) > MAX_CACHE_BYTES:
            return
        previous = self.entries.get(key.digest, b"")
        self.bytes_used += len(data) - len(previous)
        self.entries[key.digest] = data


ACTIVE_REUSE: ContextVar[EnrichmentReuse | None] = ContextVar("enrichment_reuse", default=None)


def validate_entries(artifact, entries: tuple[bytes, ...]) -> list[CachedEnrichment]:
    if len(entries) > MAX_CACHE_ENTRIES or sum(map(len, entries)) > MAX_CACHE_BYTES:
        raise ValueError("enrichment_cache_size_invalid")
    messages = {message.message_ref: message for message in artifact.projection.message_enrichments}
    parsed, seen = [], set()
    earliest_expiry = min((m.sent_at for m in messages.values()), default=None)
    if earliest_expiry is not None:
        earliest_expiry += timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
    for data in entries:
        if len(data) > MAX_ENTRY_BYTES:
            raise ValueError("enrichment_cache_entry_invalid")
        entry = CachedEnrichment.model_validate_json(data)
        source = messages.get(entry.key.message_ref)
        if source is None or entry.key.digest in seen:
            raise ValueError("enrichment_cache_source_invalid")
        if (entry.key.account_ref != source.account_ref
                or entry.key.conversation_ref != source.conversation_ref
                or entry.result() != getattr(source, entry.key.slot)
                or entry.key.expires_at.tzinfo is None
                or entry.key.expires_at < earliest_expiry
                or entry.key.expires_at > source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)):
            raise ValueError("enrichment_cache_result_invalid")
        seen.add(entry.key.digest)
        parsed.append(entry)
    return parsed


@contextmanager
def reuse_build(store, account_id, clock, cancellation, *, enabled=True):
    supported = enabled and callable(getattr(store, "load_enrichment_entries", None))
    reuse = EnrichmentReuse(store, account_id, clock, cancellation) if supported else None
    token = ACTIVE_REUSE.set(reuse)
    try:
        yield reuse
    finally:
        ACTIVE_REUSE.reset(token)
        if reuse is not None:
            reuse._batch.clear()
