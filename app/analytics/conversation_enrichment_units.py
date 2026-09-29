"""Immutable conversation-local message enrichment units for incremental projection builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence
from datetime import datetime, timedelta
from fractions import Fraction
import hashlib
import json
import zlib

from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.models.analytics import AnalyzerProvenance, ConversationMetrics, MessageEnrichment


MAX_ENRICHMENT_UNIT_BYTES = 128 * 1024 * 1024
MAX_ENRICHMENT_UNIT_RECORDS = 4_000_000


@dataclass(frozen=True, slots=True)
class ConfidenceTotal:
    count: int
    numerator: str
    denominator: str

    @classmethod
    def from_values(cls, values) -> "ConfidenceTotal":
        total = Fraction()
        count = 0
        for value in values:
            total += Fraction.from_float(float(value))
            count += 1
        return cls(count, str(total.numerator), str(total.denominator))

    def fraction(self) -> Fraction:
        return Fraction(int(self.numerator), int(self.denominator))


@dataclass(frozen=True, slots=True)
class ConversationEnrichmentUnitHeader:
    account_ref: str
    conversation_ref: str
    input_digest: str
    config_digest: str
    retention_cutoff: datetime
    expires_at: datetime
    message_count: int
    first_source_at: datetime
    last_source_at: datetime
    metrics: ConversationMetrics
    sentiment: ConfidenceTotal
    topics: ConfidenceTotal
    engagement: ConfidenceTotal
    canonical_digest: str
    analyzer_digest: str
    unit_id: str


@dataclass(frozen=True, slots=True)
class ConversationEnrichmentUnit:
    header: ConversationEnrichmentUnitHeader
    messages: bytes
    analyzers: bytes

    @property
    def retained_bytes(self) -> int:
        return len(self.messages) + len(self.analyzers)


@dataclass(frozen=True, slots=True)
class ConversationEnrichmentReference:
    generation_id: str
    header: ConversationEnrichmentUnitHeader


@dataclass(frozen=True, slots=True)
class ConversationEnrichmentProof:
    generation_id: str
    binding: str
    stamp: tuple
    headers: tuple[ConversationEnrichmentUnitHeader, ...]


@dataclass(frozen=True, slots=True)
class ConversationEnrichmentValidation:
    headers: tuple[ConversationEnrichmentUnitHeader, ...]
    proof: ConversationEnrichmentProof | None
    source_stamp: tuple | None


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def conversation_metrics_digest(metrics) -> str:
    digest = hashlib.sha256()
    for ordinal, item in enumerate(metrics):
        if ordinal:
            digest.update(b"\n")
        digest.update(_canonical(item.model_dump(mode="json")))
    return digest.hexdigest()


def _compress(raw: bytes) -> bytes:
    return zlib.compress(raw, 1)


def _compress_digest_parts(parts) -> tuple[bytes, str]:
    """Compress and hash one canonical byte stream without joining a giant prefix."""
    compressor = zlib.compressobj(1)
    digest = hashlib.sha256()
    chunks = []
    for part in parts:
        if not isinstance(part, bytes):
            raise TypeError("conversation_enrichment_frame_invalid")
        digest.update(part)
        chunk = compressor.compress(part)
        if chunk:
            chunks.append(chunk)
    tail = compressor.flush()
    if tail:
        chunks.append(tail)
    return b"".join(chunks), digest.hexdigest()


def _decompress(data: bytes, *, maximum: int) -> bytes:
    if not isinstance(data, bytes) or not data or len(data) > MAX_ENRICHMENT_UNIT_BYTES:
        raise ValueError("conversation_enrichment_unit_size_invalid")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(data, maximum + 1)
    except zlib.error as error:
        raise ValueError("conversation_enrichment_unit_encoding_invalid") from error
    if (
        len(raw) > maximum
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("conversation_enrichment_unit_expansion_invalid")
    return raw


def _unit_id(message_digest: str, analyzer_digest: str, metrics_digest: str, count: int) -> str:
    digest = hashlib.sha256(b"conversation-enrichment-unit.v1\0")
    digest.update(message_digest.encode("ascii") + b"\0")
    digest.update(analyzer_digest.encode("ascii") + b"\0")
    digest.update(metrics_digest.encode("ascii") + b"\0")
    digest.update(str(count).encode("ascii"))
    return digest.hexdigest()


def create_enrichment_unit(
    *,
    account_ref: str,
    conversation_ref: str,
    input_digest: str,
    config_digest: str,
    cutoff: datetime,
    findings,
    metrics,
    analyzer_entries,
) -> ConversationEnrichmentUnit | None:
    findings = tuple(findings)
    if (
        not findings
        or len(findings) > MAX_ENRICHMENT_UNIT_RECORDS
        or metrics.account_ref != account_ref
        or metrics.conversation_ref != conversation_ref
        or metrics.message_count != len(findings)
        or len({item.message_ref for item in findings}) != len(findings)
        or [(item.sent_at, item.source_ordinal) for item in findings]
            != sorted((item.sent_at, item.source_ordinal) for item in findings)
        or any(
            item.account_ref != account_ref
            or item.conversation_ref != conversation_ref
            or item.participant_ref != metrics.participant_ref
            or item.sent_at <= cutoff
            for item in findings
        )
    ):
        return None
    message_rows = [
        _canonical(item.model_dump(mode="json"))
        for item in findings
    ]
    message_raw = b"\n".join(message_rows)
    expires_at = findings[0].sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
    sources = {item.message_ref: item for item in findings}
    analyzer_rows = []
    from app.analytics.enrichment_cache import CachedEnrichment
    for raw in analyzer_entries:
        if isinstance(raw, bytes):
            entry = CachedEnrichment.model_validate_json(raw)
        elif isinstance(raw, str):
            entry = CachedEnrichment.model_validate_json(raw)
        else:
            entry = raw
        source = sources.get(entry.key.message_ref)
        if (
            source is None
            or entry.key.account_ref != account_ref
            or entry.key.conversation_ref != conversation_ref
            or entry.key.expires_at < expires_at
            or entry.key.expires_at
                > source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
            or entry.result() != getattr(source, entry.key.slot)
        ):
            return None
        analyzer_rows.append(_canonical(entry.model_dump(mode="json")))
    analyzer_raw = b"\n".join(analyzer_rows) if analyzer_rows else b"[]"
    messages = _compress(message_raw)
    analyzers = _compress(analyzer_raw)
    if len(messages) > MAX_ENRICHMENT_UNIT_BYTES or len(analyzers) > MAX_ENRICHMENT_UNIT_BYTES:
        return None
    message_digest = hashlib.sha256(message_raw).hexdigest()
    analyzer_digest = hashlib.sha256(analyzer_raw).hexdigest()
    metrics_bytes = _canonical(metrics.model_dump(mode="json"))
    metrics_digest = hashlib.sha256(metrics_bytes).hexdigest()
    header = ConversationEnrichmentUnitHeader(
        account_ref=account_ref,
        conversation_ref=conversation_ref,
        input_digest=input_digest,
        config_digest=config_digest,
        retention_cutoff=cutoff,
        expires_at=min(item.sent_at for item in findings)
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        message_count=len(findings),
        first_source_at=findings[0].sent_at,
        last_source_at=findings[-1].sent_at,
        metrics=metrics,
        sentiment=ConfidenceTotal.from_values(
            item.sentiment.confidence for item in findings
        ),
        topics=ConfidenceTotal.from_values(
            topic.confidence for item in findings for topic in item.topic_entities.topics
        ),
        engagement=ConfidenceTotal.from_values(
            item.engagement.confidence for item in findings
        ),
        canonical_digest=message_digest,
        analyzer_digest=analyzer_digest,
        unit_id=_unit_id(message_digest, analyzer_digest, metrics_digest, len(findings)),
    )
    return ConversationEnrichmentUnit(header, messages, analyzers)


def message_frame(unit: ConversationEnrichmentUnit) -> bytes:
    """Return digest-checked canonical bytes without allocating one object per row."""
    h = unit.header
    maximum = min(MAX_ENRICHMENT_UNIT_BYTES * 4, max(2, h.message_count * 65536))
    raw = _decompress(unit.messages, maximum=maximum)
    if hashlib.sha256(raw).hexdigest() != h.canonical_digest:
        raise ValueError("conversation_enrichment_unit_digest_invalid")
    if (raw.count(b"\n") + 1 if raw else 0) != h.message_count:
        raise ValueError("conversation_enrichment_unit_membership_invalid")
    return raw


_SENTIMENT_SCORE_MARKER = b'"score":'
_SENTIMENT_SCORE_END = frozenset(b',}')


def sentiment_score_sum(frame: bytes, expected_count: int, *, check=lambda: None) -> float:
    """Recover the exact ordered float sum without materializing message models."""
    total = 0.0
    count = 0
    offset = 0
    while True:
        start = frame.find(_SENTIMENT_SCORE_MARKER, offset)
        if start < 0:
            break
        if count % 256 == 0:
            check()
        start += len(_SENTIMENT_SCORE_MARKER)
        end = start
        while end < len(frame) and frame[end] not in _SENTIMENT_SCORE_END:
            end += 1
        if end == start or end == len(frame):
            raise ValueError('conversation_enrichment_sentiment_frame_invalid')
        try:
            total += float(frame[start:end])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('conversation_enrichment_sentiment_frame_invalid') from error
        count += 1
        if count > expected_count:
            raise ValueError('conversation_enrichment_sentiment_frame_invalid')
        offset = end + 1
    if count != expected_count:
        raise ValueError('conversation_enrichment_sentiment_frame_invalid')
    check()
    return total


def analyzer_frame(unit: ConversationEnrichmentUnit) -> bytes:
    raw = _decompress(unit.analyzers, maximum=MAX_ENRICHMENT_UNIT_BYTES * 4)
    if hashlib.sha256(raw).hexdigest() != unit.header.analyzer_digest:
        raise ValueError("conversation_enrichment_analyzer_digest_invalid")
    return raw


def message_records(unit: ConversationEnrichmentUnit):
    return tuple(message_frame(unit).splitlines())


def analyzer_records(unit: ConversationEnrichmentUnit):
    raw = analyzer_frame(unit)
    return () if raw == b"[]" else tuple(raw.splitlines())


class AppendedMessageEnrichments(Sequence):
    """Keep a verified prefix frame and materialize old models only on demand."""

    def __init__(self, rows, tail, references, first_source_at, *, previous=None):
        self._frame = rows if isinstance(rows, bytes) else None
        self._rows = None if self._frame is not None else tuple(rows)
        self._count = (previous.header.message_count if self._frame is not None and previous is not None
                       else len(self._rows or ()))
        self.tail = tail
        self.prefix_references = None if references is None else frozenset(references)
        self.first_source_at = first_source_at
        self._boundary = None
        self._previous = previous

    def __len__(self):
        return self._count + 1

    @property
    def prefix_frame(self):
        return self._frame

    def _row(self, index):
        if self._rows is None:
            if index == self._count - 1:
                return self._frame.rpartition(b"\n")[2]
            self._rows = tuple(self._frame.splitlines())
            if len(self._rows) != self._count:
                raise ValueError("conversation_enrichment_unit_membership_invalid")
        return self._rows[index]

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index == self._count:
            return self.tail
        if index == self._count - 1:
            if self._boundary is None:
                self._boundary = MessageEnrichment.model_validate_json(self._row(index))
            return self._boundary
        return MessageEnrichment.model_validate_json(self._row(index))


class InsertedMessageEnrichments(Sequence):
    """Keep verified rows and materialize only explicitly requested suffix models."""

    def __init__(self, rows, inserted, index, first_source_at):
        self.rows = tuple(rows)
        self.inserted = inserted
        self.index = index
        self.first_source_at = first_source_at

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index == self.index:
            return self.inserted
        return MessageEnrichment.model_validate_json(self.rows[index])


class IncrementalMessageEnrichments:
    """Internal sequence that streams canonical rows without retaining message models."""

    def __init__(self, account_ref, units, store):
        self.account_ref = account_ref
        self.units = tuple(units)
        self.store = store
        self._loaded = {}

    def __len__(self):
        return sum(unit.header.message_count for unit in self.units)

    @property
    def first_source_at(self):
        return min((unit.header.first_source_at for unit in self.units), default=None)

    @property
    def last_source_at(self):
        return max((unit.header.last_source_at for unit in self.units), default=None)

    def _contents(self):
        missing = [
            unit.header.unit_id
            for unit in self.units
            if isinstance(unit, ConversationEnrichmentReference)
            and unit.header.unit_id not in self._loaded
        ]
        if missing:
            self._loaded.update(self.store.load_enrichment_unit_contents(self.account_ref, missing))
        for value in self.units:
            if isinstance(value, ConversationEnrichmentUnit):
                yield value
                continue
            content = self._loaded.get(value.header.unit_id)
            if content is None:
                raise ValueError("conversation_enrichment_reference_changed")
            yield ConversationEnrichmentUnit(value.header, content[0], content[1])

    def digest_components(self):
        return tuple(
            (unit.header.conversation_ref, unit.header.message_count,
             unit.header.canonical_digest)
            for unit in self.units
        )

    def iter_canonical_records(self, check=lambda: None):
        for unit in self._contents():
            for raw in message_records(unit):
                check()
                yield raw.decode("utf-8")

    def __iter__(self):
        for raw in self.iter_canonical_records():
            yield MessageEnrichment.model_validate_json(raw)

    def validation_messages_for(self, references, *, check=lambda: None):
        """Read actual row identities and dates; model only requested cache sources."""
        messages, first = {}, None
        for unit in self.units:
            if not isinstance(unit, ConversationEnrichmentUnit):
                continue
            for raw in message_records(unit):
                check()
                value = json.loads(raw)
                item = None
                try:
                    at = datetime.fromisoformat(value['sent_at'])
                    if at.tzinfo is None or at.utcoffset() is None:
                        raise ValueError('naive_source_time')
                except (TypeError, ValueError):
                    item = MessageEnrichment.model_validate_json(raw)
                    at = item.sent_at
                first = at if first is None or at < first else first
                if value['message_ref'] in references:
                    item = item or MessageEnrichment.model_validate_json(raw)
                    messages[item.message_ref] = item
        check()
        return messages, first

    def validation_messages(self):
        result = {}
        for unit in self.units:
            if not isinstance(unit, ConversationEnrichmentUnit):
                continue
            for raw in message_records(unit):
                item = MessageEnrichment.model_validate_json(raw)
                result[item.message_ref] = item
        return result

    def provenance(self, descriptors: tuple[AnalyzerProvenance, ...]):
        eligible = len(self)
        totals = []
        for name in ("sentiment", "topics", "engagement"):
            count = 0
            total = Fraction()
            for unit in self.units:
                value = getattr(unit.header, name)
                count += value.count
                total += value.fraction()
            totals.append((count, total))
        result = []
        for descriptor, (count, total) in zip(descriptors, totals, strict=True):
            result.append(descriptor.model_copy(update={
                "analyzed_sample_count": eligible,
                "eligible_sample_count": eligible,
                "sample_coverage": 1.0 if eligible else None,
                "mean_confidence": round(float(total / count), 6) if count else None,
                "unavailable_reason": None if eligible else "no_eligible_samples",
            }))
        return result


def append_enrichment_unit(previous, *, input_digest, config_digest, cutoff,
                           findings, metrics, analyzer_entries, check=lambda: None):
    """Copy an already verified prefix; normal stored-unit validation still follows."""
    from app.analytics.enrichment_cache import (
        CachedEnrichment, MAX_CONVERSATION_CACHE_BYTES, MAX_CONVERSATION_CACHE_ENTRIES,
    )
    h = previous.header
    if (len(findings) != h.message_count + 1
            or len(findings) > MAX_ENRICHMENT_UNIT_RECORDS
            or metrics.account_ref != h.account_ref
            or metrics.conversation_ref != h.conversation_ref
            or metrics.participant_ref != h.metrics.participant_ref
            or metrics.message_count != len(findings)
            or config_digest != h.config_digest
            or h.retention_cutoff > cutoff or h.first_source_at <= cutoff):
        return None
    tail = findings[-1]
    if (tail.account_ref != h.account_ref or tail.conversation_ref != h.conversation_ref
            or tail.participant_ref != h.metrics.participant_ref or tail.sent_at <= cutoff
            or (tail.sent_at, tail.source_ordinal) < (findings[-2].sent_at, findings[-2].source_ordinal)
            or (tail.message_ref in findings.prefix_references
                if isinstance(findings, AppendedMessageEnrichments) and findings.prefix_references is not None
                else False if isinstance(findings, AppendedMessageEnrichments) and findings.prefix_frame is not None
                else any(item.message_ref == tail.message_ref for item in findings[:-1]))):
        return None
    check()
    prefix_frame = (findings.prefix_frame if isinstance(findings, AppendedMessageEnrichments)
                    and findings._previous is previous else None)
    if prefix_frame is None:
        prefix_frame = message_frame(previous)
    tail_raw = _canonical(tail.model_dump(mode='json'))
    messages, message_digest = _compress_digest_parts((prefix_frame, b'\n', tail_raw))
    previous_analyzers = analyzer_frame(previous)
    analyzer_count = 0 if previous_analyzers == b'[]' else previous_analyzers.count(b'\n') + 1
    analyzer_size = 0 if previous_analyzers == b'[]' else len(previous_analyzers)
    added_rows = []
    added_keys = set()
    for raw in analyzer_entries:
        check()
        entry = CachedEnrichment.model_validate_json(raw)
        key = entry.key
        if (key.account_ref != h.account_ref or key.conversation_ref != h.conversation_ref
                or key.message_ref != tail.message_ref or key.digest in added_keys
                or not h.expires_at <= key.expires_at <= tail.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                or entry.result() != getattr(tail, key.slot)):
            raise ValueError('conversation_enrichment_append_analyzer_invalid')
        added_keys.add(key.digest)
        encoded = _canonical(entry.model_dump(mode='json'))
        if (analyzer_count + len(added_rows) < MAX_CONVERSATION_CACHE_ENTRIES
                and analyzer_size + sum(map(len, added_rows)) + len(encoded) <= MAX_CONVERSATION_CACHE_BYTES):
            added_rows.append(encoded)
    check()
    base = b'' if previous_analyzers == b'[]' else previous_analyzers
    analyzer_parts = []
    if base:
        analyzer_parts.append(base)
    if base and added_rows:
        analyzer_parts.append(b'\n')
    if added_rows:
        analyzer_parts.append(b'\n'.join(added_rows))
    if not analyzer_parts:
        analyzer_parts.append(b'[]')
    analyzers, analyzer_digest = _compress_digest_parts(analyzer_parts)
    if max(len(messages), len(analyzers)) > MAX_ENRICHMENT_UNIT_BYTES:
        return None
    metrics_digest = hashlib.sha256(_canonical(metrics.model_dump(mode='json'))).hexdigest()
    def total(previous_total, values):
        extra = ConfidenceTotal.from_values(values)
        value = previous_total.fraction() + extra.fraction()
        return ConfidenceTotal(previous_total.count + extra.count, str(value.numerator), str(value.denominator))
    header = ConversationEnrichmentUnitHeader(
        account_ref=h.account_ref, conversation_ref=h.conversation_ref,
        input_digest=input_digest, config_digest=config_digest, retention_cutoff=cutoff,
        expires_at=h.expires_at, message_count=h.message_count + 1,
        first_source_at=h.first_source_at, last_source_at=tail.sent_at, metrics=metrics,
        sentiment=total(h.sentiment, [tail.sentiment.confidence]),
        topics=total(h.topics, [item.confidence for item in tail.topic_entities.topics]),
        engagement=total(h.engagement, [tail.engagement.confidence]),
        canonical_digest=message_digest, analyzer_digest=analyzer_digest,
        unit_id=_unit_id(message_digest, analyzer_digest, metrics_digest, h.message_count + 1),
    )
    check()
    return ConversationEnrichmentUnit(header, messages, analyzers)
