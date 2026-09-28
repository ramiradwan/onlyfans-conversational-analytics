"""Independently validate stored single-insertion enrichment selections."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

from app.analytics.conversation_enrichment_units import (
    ConfidenceTotal, ConversationEnrichmentUnit, message_records, analyzer_records,
    MAX_ENRICHMENT_UNIT_BYTES, MAX_ENRICHMENT_UNIT_RECORDS, _canonical, _unit_id, _compress,
)
from app.analytics.conversation_pages import PAGE_RECORDS
from app.analytics.enrichment_cache import CachedEnrichment, MAX_CONVERSATION_CACHE_BYTES, MAX_CONVERSATION_CACHE_ENTRIES
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS


def increase(total, values):
    added = ConfidenceTotal.from_values(values)
    value = total.fraction() + added.fraction()
    return ConfidenceTotal(total.count + added.count, str(value.numerator), str(value.denominator))


def pack_insertion(previous, findings, metrics, input_digest, config, cutoff, entries, check):
    h = previous.header
    new = findings.inserted
    if (len(findings) != h.message_count+1 or len(findings) > MAX_ENRICHMENT_UNIT_RECORDS
            or config != h.config_digest or cutoff < h.retention_cutoff
            or h.first_source_at <= cutoff or new.sent_at <= cutoff
            or new.account_ref != h.account_ref or new.conversation_ref != h.conversation_ref
            or new.participant_ref != h.metrics.participant_ref or metrics.message_count != len(findings)
            or metrics.account_ref != h.account_ref or metrics.conversation_ref != h.conversation_ref
            or metrics.participant_ref != h.metrics.participant_ref):
        return None
    old_analyzers = analyzer_records(previous)
    analyzer_rows = list(old_analyzers)
    size = sum(map(len, analyzer_rows))
    seen = set()
    for raw in entries:
        check()
        entry = CachedEnrichment.model_validate_json(raw)
        k = entry.key
        if (k.message_ref != new.message_ref or k.account_ref != h.account_ref
                or k.conversation_ref != h.conversation_ref or k.digest in seen
                or not h.expires_at <= k.expires_at <= new.sent_at+timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                or entry.result() != getattr(new, k.slot)):
            raise ValueError('conversation_insertion_analyzer_invalid')
        seen.add(k.digest)
        encoded = _canonical(entry.model_dump(mode='json'))
        if len(analyzer_rows) < MAX_CONVERSATION_CACHE_ENTRIES and size+len(encoded) <= MAX_CONVERSATION_CACHE_BYTES:
            analyzer_rows.append(encoded)
            size += len(encoded)
    check()
    message_raw = b'\n'.join(findings.rows)
    analyzer_raw = b'\n'.join(analyzer_rows) if analyzer_rows else b'[]'
    messages, analyzers = _compress(message_raw), _compress(analyzer_raw)
    if max(len(messages), len(analyzers)) > MAX_ENRICHMENT_UNIT_BYTES:
        return None
    message_digest = hashlib.sha256(message_raw).hexdigest()
    analyzer_digest = hashlib.sha256(analyzer_raw).hexdigest()
    metrics_digest = hashlib.sha256(_canonical(metrics.model_dump(mode='json'))).hexdigest()
    header = replace(h, input_digest=input_digest, config_digest=config, retention_cutoff=cutoff,
        message_count=len(findings), metrics=metrics,
        sentiment=increase(h.sentiment, [new.sentiment.confidence]),
        topics=increase(h.topics, [t.confidence for t in new.topic_entities.topics]),
        engagement=increase(h.engagement, [new.engagement.confidence]),
        canonical_digest=message_digest, analyzer_digest=analyzer_digest,
        unit_id=_unit_id(message_digest, analyzer_digest, metrics_digest, len(findings)))
    check()
    return ConversationEnrichmentUnit(header, messages, analyzers)


def validate_inserted_unit(connection, previous_generation, previous, unit, *, check):
    """No constructor hint is trusted: read and compare the actual persisted rows."""
    from app.analytics.conversation_enrichment_unit_sql import load_unit
    from app.analytics.metrics import build_conversation_metrics_from_bound_values
    from app.analytics.conversation_insertion import metric_input
    from app.models.analytics import MessageEnrichment
    h = unit.header
    if (previous is None or not previous.message_count or h.message_count > MAX_ENRICHMENT_UNIT_RECORDS
            or h.message_count != previous.message_count+1
            or h.account_ref != previous.account_ref or h.conversation_ref != previous.conversation_ref
            or h.config_digest != previous.config_digest or h.retention_cutoff < previous.retention_cutoff
            or h.first_source_at <= h.retention_cutoff or h.first_source_at != previous.first_source_at
            or h.last_source_at != previous.last_source_at or h.expires_at != previous.expires_at
            or h.metrics.account_ref != h.account_ref or h.metrics.conversation_ref != h.conversation_ref
            or h.metrics.participant_ref != previous.metrics.participant_ref
            or h.metrics.message_count != h.message_count or h.metrics.unread_count != previous.metrics.unread_count):
        return False
    check()
    old = load_unit(connection, previous_generation, h.account_ref, h.conversation_ref)
    if old is None or old.header != previous:
        return False
    before, after = message_records(old), message_records(unit)
    insertion = None
    for index, (left, right) in enumerate(zip(before, after)):
        check()
        if left != right:
            insertion = index
            break
    if insertion is None or len(before)-insertion > PAGE_RECORDS:
        return False
    added = MessageEnrichment.model_validate_json(after[insertion])
    if (added.account_ref != h.account_ref or added.conversation_ref != h.conversation_ref
            or added.participant_ref != h.metrics.participant_ref or added.sent_at <= h.retention_cutoff
            or added.source_ordinal != insertion):
        return False
    values, seen = [], set()
    for index, raw in enumerate(before):
        check()
        value = json.loads(raw)
        if (type(value['source_ordinal']) is not int or value['source_ordinal'] != index
                or not isinstance(value['sent_at'], str)
                or value['message_ref'] == added.message_ref or value['message_ref'] in seen):
            return False
        seen.add(value['message_ref'])
        position = index + (index >= insertion)
        current = value if index < insertion else dict(value, source_ordinal=position)
        if (raw if index < insertion else _canonical(current)) != after[position]:
            return False
        values.append(metric_input(current))
    if added.sent_at != values[insertion].sent_at:
        return False
    from app.analytics.metrics import ConversationMetricInput
    values.insert(insertion, ConversationMetricInput.from_enrichment(added))
    metrics = build_conversation_metrics_from_bound_values(h.account_ref, h.conversation_ref,
        h.metrics.participant_ref, previous.metrics.unread_count, values)
    if metrics != h.metrics:
        return False
    if (increase(previous.sentiment,[added.sentiment.confidence]) != h.sentiment
            or increase(previous.topics,[t.confidence for t in added.topic_entities.topics]) != h.topics
            or increase(previous.engagement,[added.engagement.confidence]) != h.engagement):
        return False
    before_analyzers, after_analyzers = analyzer_records(old), analyzer_records(unit)
    if not len(before_analyzers) <= len(after_analyzers) <= len(before_analyzers)+3:
        return False
    for left, right in zip(before_analyzers, after_analyzers):
        check()
        if left != right:
            return False
    keys = set()
    for raw in after_analyzers[len(before_analyzers):]:
        check()
        entry = CachedEnrichment.model_validate_json(raw)
        k = entry.key
        if (k.message_ref != added.message_ref or k.account_ref != h.account_ref
                or k.conversation_ref != h.conversation_ref or k.digest in keys
                or not h.expires_at <= k.expires_at <= added.sent_at+timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                or entry.result() != getattr(added,k.slot)):
            return False
        keys.add(k.digest)
    metrics_digest = hashlib.sha256(_canonical(h.metrics.model_dump(mode='json'))).hexdigest()
    if _unit_id(h.canonical_digest, h.analyzer_digest, metrics_digest, h.message_count) != h.unit_id:
        return False
    check()
    return True
