"""Immutable conversation-local message enrichment units for incremental projection builds."""

from __future__ import annotations

from dataclasses import dataclass, field
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


def message_records(unit: ConversationEnrichmentUnit):
    h = unit.header
    maximum = min(MAX_ENRICHMENT_UNIT_BYTES * 4, max(2, h.message_count * 65536))
    raw = _decompress(unit.messages, maximum=maximum)
    if hashlib.sha256(raw).hexdigest() != h.canonical_digest:
        raise ValueError("conversation_enrichment_unit_digest_invalid")
    rows = raw.splitlines()
    if len(rows) != h.message_count:
        raise ValueError("conversation_enrichment_unit_membership_invalid")
    return tuple(rows)


def analyzer_records(unit: ConversationEnrichmentUnit):
    raw = _decompress(unit.analyzers, maximum=MAX_ENRICHMENT_UNIT_BYTES * 4)
    if hashlib.sha256(raw).hexdigest() != unit.header.analyzer_digest:
        raise ValueError("conversation_enrichment_analyzer_digest_invalid")
    return () if raw == b"[]" else tuple(raw.splitlines())


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
