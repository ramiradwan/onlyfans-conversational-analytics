"""Versioned, bounded reuse records for independent message analyzers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
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
MAX_CONVERSATION_CACHE_ENTRIES = 30_000
MAX_CONVERSATION_CACHE_BYTES = 16 * 1024 * 1024
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


def _key_from_digests(message, slot, context, *, input_digest, adapter_digest,
                      context_digest) -> EnrichmentKey:
    dependencies = (message, *context)
    return EnrichmentKey(
        account_ref=account_ref(message.creator_account_id),
        conversation_ref=conversation_ref(message.creator_account_id, message.conversation_id),
        message_ref=message_ref(message.creator_account_id, message.conversation_id, message.message_id),
        slot=slot, input_digest=input_digest, adapter_digest=adapter_digest,
        context_digest=context_digest,
        expires_at=min(item.sent_at for item in dependencies).astimezone(timezone.utc)
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
    )


def enrichment_key(message, slot, descriptor, policy, context) -> EnrichmentKey:
    return _key_from_digests(message, slot, context,
        input_digest=fingerprint(input_document(message)),
        adapter_digest=fingerprint({"descriptor": descriptor.model_dump(mode="json"),
                                    "policy": policy.model_dump(mode="json")}),
        context_digest=fingerprint([input_document(item) for item in context]))


class EnrichmentKeyBatch:
    """Share input digests within one lookup batch, never across builds."""

    def __init__(self, messages: dict[int, MessageAnalysisInput]):
        if len(messages) > CACHE_BATCH_SIZE + 64:
            raise ValueError("enrichment_input_batch_invalid")
        self._messages = messages
        self._documents: dict[int, dict] = {}
        self._inputs: dict[int, str] = {}
        self._contexts: dict[tuple[int, ...], str] = {}

    def _document(self, index: int) -> dict:
        if index not in self._documents:
            self._documents[index] = input_document(self._messages[index])
        return self._documents[index]

    def key(self, index: int, slot: Slot, adapter_digest: str,
            context_indices: tuple[int, ...]) -> EnrichmentKey:
        if index not in self._inputs:
            self._inputs[index] = fingerprint(self._document(index))
        if context_indices not in self._contexts:
            self._contexts[context_indices] = fingerprint(
                [self._document(other) for other in context_indices])
        return _key_from_digests(self._messages[index], slot,
            tuple(self._messages[other] for other in context_indices),
            input_digest=self._inputs[index], adapter_digest=adapter_digest,
            context_digest=self._contexts[context_indices])


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
        self._conversation_keys: dict[str, list[str]] = {}
        self._local_conversation_ref: str | None = None
        self._local_conversation_entries: dict[str, bytes] = {}
        self._local_conversation_bytes = 0
        self._fallback_conversation_ref: str | None = None
        self._fallback_conversation_entries: dict[str, bytes] = {}

    def prefetch(self, keys: list[EnrichmentKey]) -> None:
        check_cancelled(self.cancellation)
        self._batch = self.store.load_enrichment_entries(
            self.account_id, [key.digest for key in keys],
            now=self.clock(), cancellation_check=self.cancellation,
        ) if keys else {}
        if not keys or len(self._batch) == len(keys):
            return
        references = {key.conversation_ref for key in keys if key.digest not in self._batch}
        loader = getattr(self.store, "load_conversation_enrichment_entries", None)
        if len(references) != 1 or not callable(loader):
            return
        reference = references.pop()
        if self._fallback_conversation_ref != reference:
            self._fallback_conversation_entries = loader(
                self.account_id, reference, now=self.clock(),
                cancellation_check=self.cancellation,
            )
            self._fallback_conversation_ref = reference
        for key in keys:
            check_cancelled(self.cancellation)
            if key.digest not in self._batch:
                data = self._fallback_conversation_entries.get(key.digest)
                if data is not None:
                    self._batch[key.digest] = data

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
        if key.expires_at <= self.clock():
            return
        result_json = result.model_dump_json()
        if len(result_json.encode("utf-8")) > MAX_ENTRY_BYTES:
            return
        self.retain_record(CachedEnrichment(key=key, result_json=result_json))

    def retain_record(self, entry: CachedEnrichment) -> None:
        """Retain a checked conversation result; staging checks its source again."""

        check_cancelled(self.cancellation)
        key = entry.key
        if key.expires_at <= self.clock():
            return
        data = entry.model_dump_json().encode("utf-8")
        if len(data) > MAX_ENTRY_BYTES:
            return
        signature = key.digest
        if self._local_conversation_ref != key.conversation_ref:
            self._local_conversation_ref = key.conversation_ref
            self._local_conversation_entries.clear()
            self._local_conversation_bytes = 0
        local_previous = self._local_conversation_entries.get(signature, b"")
        local_size = self._local_conversation_bytes + len(data) - len(local_previous)
        if (signature in self._local_conversation_entries
                or (len(self._local_conversation_entries) < MAX_CONVERSATION_CACHE_ENTRIES
                    and local_size <= MAX_CONVERSATION_CACHE_BYTES)):
            self._local_conversation_entries[signature] = data
            self._local_conversation_bytes = local_size
        if len(self.entries) >= MAX_CACHE_ENTRIES or self.bytes_used + len(data) > MAX_CACHE_BYTES:
            return
        previous = self.entries.get(signature, b"")
        self.bytes_used += len(data) - len(previous)
        if signature not in self.entries:
            self._conversation_keys.setdefault(key.conversation_ref, []).append(signature)
        self.entries[signature] = data

    def conversation_entries(self, reference: str) -> tuple[bytes, ...]:
        if self._local_conversation_ref == reference:
            return tuple(self._local_conversation_entries.values())
        return tuple(self.entries[key] for key in self._conversation_keys.get(reference, ()))


ACTIVE_REUSE: ContextVar[EnrichmentReuse | None] = ContextVar("enrichment_reuse", default=None)


def _checked_entries(artifact, entries: tuple[bytes, ...], *,
                     check: Callable[[], None] = lambda: None) -> Iterator[tuple[CachedEnrichment, str]]:
    check()
    if len(entries) > MAX_CACHE_ENTRIES or sum(map(len, entries)) > MAX_CACHE_BYTES:
        raise ValueError("enrichment_cache_size_invalid")
    source = artifact.projection.message_enrichments
    local_messages = getattr(source, "validation_messages", None)
    messages = (
        local_messages()
        if callable(local_messages)
        else {message.message_ref: message for message in source}
    )
    seen = set()
    earliest_expiry = min((m.sent_at for m in messages.values()), default=None)
    if earliest_expiry is not None:
        earliest_expiry += timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
    check()
    for data in entries:
        check()
        if len(data) > MAX_ENTRY_BYTES:
            raise ValueError("enrichment_cache_entry_invalid")
        entry = CachedEnrichment.model_validate_json(data)
        source = messages.get(entry.key.message_ref)
        signature = entry.key.digest
        if source is None or signature in seen:
            raise ValueError("enrichment_cache_source_invalid")
        if (entry.key.account_ref != source.account_ref
                or entry.key.conversation_ref != source.conversation_ref
                or entry.result() != getattr(source, entry.key.slot)
                or entry.key.expires_at.tzinfo is None
                or entry.key.expires_at < earliest_expiry
                or entry.key.expires_at > source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)):
            raise ValueError("enrichment_cache_result_invalid")
        seen.add(signature)
        yield entry, signature
    check()


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
            reuse._fallback_conversation_entries.clear()


@dataclass(frozen=True, slots=True)
class EnrichmentStorageRecord:
    account_ref: str
    cache_key: str
    expires_at: str
    document_json: str
    document_digest: str


def validate_entries(artifact, entries: tuple[bytes, ...]) -> list[CachedEnrichment]:
    """Return checked models for stores that retain model-backed entries."""

    return [entry for entry, _ in _checked_entries(artifact, entries)]


def storage_entries(artifact, entries: tuple[bytes, ...], *,
                    check: Callable[[], None] = lambda: None) -> Iterator[EnrichmentStorageRecord]:
    """Prepare one immutable SQL record after checking it against the artifact."""

    for entry, signature in _checked_entries(artifact, entries, check=check):
        document = entry.model_dump_json()
        encoded = document.encode('utf-8')
        if len(encoded) > MAX_ENTRY_BYTES:
            raise ValueError("enrichment_cache_entry_invalid")
        yield EnrichmentStorageRecord(entry.key.account_ref, signature,
            entry.key.expires_at.isoformat(), document,
            hashlib.sha256(encoded).hexdigest())
