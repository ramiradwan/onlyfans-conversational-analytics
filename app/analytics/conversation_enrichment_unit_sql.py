"""Persist and reopen immutable conversation-local enrichment units."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from app.analytics.conversation_enrichment_units import (
    ConfidenceTotal,
    ConversationEnrichmentReference,
    ConversationEnrichmentUnit,
    ConversationEnrichmentUnitHeader,
    analyzer_records,
)
from app.analytics.enrichment_cache import CachedEnrichment
from app.models.analytics import ConversationMetrics


def supported(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='conversation_enrichment_refs'"
    ).fetchone() is not None


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("conversation_enrichment_unit_time_invalid")
    return result.astimezone(timezone.utc)


def _total(row, prefix: str) -> ConfidenceTotal:
    return ConfidenceTotal(
        int(row[prefix + "_count"]),
        str(row[prefix + "_numerator"]),
        str(row[prefix + "_denominator"]),
    )


def _header(row) -> ConversationEnrichmentUnitHeader:
    return ConversationEnrichmentUnitHeader(
        account_ref=row["creator_account_id"],
        conversation_ref=row["conversation_ref"],
        input_digest=row["input_digest"],
        config_digest=row["config_digest"],
        retention_cutoff=_instant(row["retention_cutoff"]),
        expires_at=_instant(row["expires_at"]),
        message_count=int(row["message_count"]),
        first_source_at=_instant(row["first_source_at"]),
        last_source_at=_instant(row["last_source_at"]),
        metrics=ConversationMetrics.model_validate_json(row["metrics_json"]),
        sentiment=_total(row, "sentiment"),
        topics=_total(row, "topic"),
        engagement=_total(row, "engagement"),
        canonical_digest=row["canonical_digest"],
        analyzer_digest=row["analyzer_digest"],
        unit_id=row["unit_id"],
    )


_SELECT = """SELECT r.*,u.message_count,u.first_source_at,u.last_source_at,u.metrics_json,
    u.sentiment_count,u.sentiment_numerator,u.sentiment_denominator,
    u.topic_count,u.topic_numerator,u.topic_denominator,
    u.engagement_count,u.engagement_numerator,u.engagement_denominator,
    u.canonical_digest,u.analyzer_digest"""


def load_reference(connection, generation_id, account, conversation,
                   input_digest, config_digest):
    row = connection.execute(
        _SELECT + """
           FROM conversation_enrichment_refs r
           JOIN conversation_enrichment_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=?
             AND r.conversation_ref=? AND r.input_digest=? AND r.config_digest=?""",
        (generation_id, account, conversation, input_digest, config_digest),
    ).fetchone()
    return None if row is None else ConversationEnrichmentReference(
        generation_id, _header(row)
    )


def load_unit(connection, generation_id, account, conversation):
    row = connection.execute(
        _SELECT + """,u.message_bytes,u.analyzer_bytes
           FROM conversation_enrichment_refs r
           JOIN conversation_enrichment_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=?
             AND r.conversation_ref=?""",
        (generation_id, account, conversation),
    ).fetchone()
    if row is None:
        return None
    return ConversationEnrichmentUnit(
        _header(row), row["message_bytes"], row["analyzer_bytes"]
    )


def load_contents(connection, account, unit_ids, *, check=lambda: None):
    result = {}
    values = list(dict.fromkeys(unit_ids))
    for offset in range(0, len(values), 128):
        check()
        batch = values[offset:offset + 128]
        marks = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT unit_id,message_bytes,analyzer_bytes
                FROM conversation_enrichment_units
                WHERE creator_account_id=? AND unit_id IN ({marks})
                ORDER BY unit_id""",
            (account, *batch),
        )
        for row in rows:
            check()
            result[row["unit_id"]] = (row["message_bytes"], row["analyzer_bytes"])
    return result


def load_analyzer_entries(connection, generation_id, account, conversation):
    unit = load_unit(connection, generation_id, account, conversation)
    if unit is None:
        return {}
    result = {}
    try:
        for raw in analyzer_records(unit):
            item = CachedEnrichment.model_validate_json(raw)
            if (item.key.account_ref != account
                    or item.key.conversation_ref != conversation):
                return {}
            result[item.key.digest] = raw
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}
    return result


def insert_units(connection, generation_id: str, values, *, check=lambda: None) -> int:
    if not connection.in_transaction:
        raise ValueError("conversation_enrichment_units_require_transaction")
    if not supported(connection):
        return 0
    inserted = 0
    for ordinal, value in enumerate(values):
        check()
        h = value.header
        if isinstance(value, ConversationEnrichmentReference):
            cursor = connection.execute(
                """INSERT INTO conversation_enrichment_refs
                   (generation_id,creator_account_id,conversation_ref,ordinal,input_digest,
                    config_digest,retention_cutoff,expires_at,unit_id)
                   SELECT ?,creator_account_id,conversation_ref,?,input_digest,
                          config_digest,retention_cutoff,expires_at,unit_id
                   FROM conversation_enrichment_refs
                   WHERE generation_id=? AND creator_account_id=?
                     AND conversation_ref=? AND input_digest=? AND config_digest=?
                     AND unit_id=?""",
                (
                    generation_id, ordinal, value.generation_id, h.account_ref,
                    h.conversation_ref, h.input_digest, h.config_digest, h.unit_id,
                ),
            )
            inserted += max(0, cursor.rowcount)
            continue
        if not isinstance(value, ConversationEnrichmentUnit):
            raise TypeError("conversation_enrichment_unit_invalid")
        row = connection.execute(
            """SELECT * FROM conversation_enrichment_units
               WHERE creator_account_id=? AND unit_id=?""",
            (h.account_ref, h.unit_id),
        ).fetchone()
        exact = (
            row is not None
            and int(row["message_count"]) == h.message_count
            and ConversationMetrics.model_validate_json(row["metrics_json"]) == h.metrics
            and row["canonical_digest"] == h.canonical_digest
            and row["analyzer_digest"] == h.analyzer_digest
            and row["message_bytes"] == value.messages
            and row["analyzer_bytes"] == value.analyzers
        )
        if row is not None and not exact:
            continue
        if row is None:
            connection.execute(
                """INSERT INTO conversation_enrichment_units
                   (creator_account_id,unit_id,message_count,first_source_at,last_source_at,metrics_json,
                    sentiment_count,sentiment_numerator,sentiment_denominator,
                    topic_count,topic_numerator,topic_denominator,
                    engagement_count,engagement_numerator,engagement_denominator,
                    canonical_digest,analyzer_digest,message_bytes,analyzer_bytes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    h.account_ref, h.unit_id, h.message_count,
                    h.first_source_at.isoformat(), h.last_source_at.isoformat(),
                    h.metrics.model_dump_json(),
                    h.sentiment.count, h.sentiment.numerator, h.sentiment.denominator,
                    h.topics.count, h.topics.numerator, h.topics.denominator,
                    h.engagement.count, h.engagement.numerator, h.engagement.denominator,
                    h.canonical_digest, h.analyzer_digest, value.messages, value.analyzers,
                ),
            )
        connection.execute(
            """INSERT INTO conversation_enrichment_refs
               (generation_id,creator_account_id,conversation_ref,ordinal,input_digest,
                config_digest,retention_cutoff,expires_at,unit_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                generation_id, h.account_ref, h.conversation_ref, ordinal,
                h.input_digest, h.config_digest, h.retention_cutoff.isoformat(),
                h.expires_at.isoformat(), h.unit_id,
            ),
        )
        inserted += 1
    check()
    return inserted


def _validate_unit(unit, *, check=lambda: None, materialize=False):
    from app.analytics.conversation_enrichment_units import (
        ConfidenceTotal, _canonical, _unit_id, message_records,
    )
    from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
    from app.models.analytics import MessageEnrichment
    from datetime import timedelta
    import hashlib

    h = unit.header
    messages = []
    sources = {}
    seen = set()
    sentiment, topics, engagement = [], [], []
    previous = None
    first_seen = last_seen = None
    for raw in message_records(unit):
        check()
        item = MessageEnrichment.model_validate_json(raw)
        current = (item.sent_at, item.source_ordinal)
        if (
            item.account_ref != h.account_ref
            or item.conversation_ref != h.conversation_ref
            or item.participant_ref != h.metrics.participant_ref
            or item.sent_at <= h.retention_cutoff
            or item.message_ref in seen
            or (previous is not None and current < previous)
        ):
            raise ValueError("conversation_enrichment_unit_source_invalid")
        seen.add(item.message_ref)
        sources[item.message_ref] = item
        previous = current
        first_seen = item.sent_at if first_seen is None else min(first_seen, item.sent_at)
        last_seen = item.sent_at if last_seen is None else max(last_seen, item.sent_at)
        sentiment.append(item.sentiment.confidence)
        topics.extend(topic.confidence for topic in item.topic_entities.topics)
        engagement.append(item.engagement.confidence)
        if materialize:
            messages.append(item)

    metrics_digest = hashlib.sha256(
        _canonical(h.metrics.model_dump(mode="json"))
    ).hexdigest()
    if (
        len(seen) != h.message_count
        or h.metrics.account_ref != h.account_ref
        or h.metrics.conversation_ref != h.conversation_ref
        or h.metrics.message_count != h.message_count
        or first_seen != h.first_source_at
        or last_seen != h.last_source_at
        or h.expires_at
            != h.first_source_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
        or ConfidenceTotal.from_values(sentiment) != h.sentiment
        or ConfidenceTotal.from_values(topics) != h.topics
        or ConfidenceTotal.from_values(engagement) != h.engagement
        or _unit_id(
            h.canonical_digest, h.analyzer_digest, metrics_digest, h.message_count
        ) != h.unit_id
    ):
        raise ValueError("conversation_enrichment_unit_summary_invalid")

    analyzer_seen = set()
    for raw in analyzer_records(unit):
        check()
        entry = CachedEnrichment.model_validate_json(raw)
        source = sources.get(entry.key.message_ref)
        due = (
            None if source is None else
            source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
        )
        if (
            source is None
            or entry.key.account_ref != h.account_ref
            or entry.key.conversation_ref != h.conversation_ref
            or entry.key.digest in analyzer_seen
            or not h.expires_at <= entry.key.expires_at <= due
            or entry.result() != getattr(source, entry.key.slot)
        ):
            raise ValueError("conversation_enrichment_analyzer_invalid")
        analyzer_seen.add(entry.key.digest)
    return messages


def verify_generation_units(
    connection, generation_id, account, validation=None, *,
    check=lambda: None, materialize=False,
):
    """Verify schema-17 enrichment units and return digest components/messages."""

    from app.analytics.conversation_enrichment_units import ConversationEnrichmentUnit
    from app.analytics.validation_receipt import content_stamp

    rows = connection.execute(
        _SELECT + """
           FROM conversation_enrichment_refs r
           JOIN conversation_enrichment_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=?
           ORDER BY r.ordinal""",
        (generation_id, account),
    ).fetchall()
    headers = tuple(_header(row) for row in rows)
    if validation is not None and headers != tuple(validation.headers):
        raise ValueError("conversation_enrichment_manifest_changed")

    trusted = {}
    if (
        validation is not None
        and validation.proof is not None
        and validation.source_stamp is not None
        and tuple(validation.source_stamp) == validation.proof.stamp
    ):
        trusted = {
            h.conversation_ref: h for h in validation.proof.headers
        }

    components, messages = [], []
    for row, header in zip(rows, headers, strict=True):
        check()
        components.append(
            (header.conversation_ref, header.message_count, header.canonical_digest)
        )
        if trusted.get(header.conversation_ref) == header and not materialize:
            continue
        content = connection.execute(
            """SELECT message_bytes,analyzer_bytes
               FROM conversation_enrichment_units
               WHERE creator_account_id=? AND unit_id=?""",
            (account, header.unit_id),
        ).fetchone()
        if content is None:
            raise ValueError("conversation_enrichment_unit_absent")
        unit = ConversationEnrichmentUnit(
            header, content["message_bytes"], content["analyzer_bytes"]
        )
        messages.extend(
            _validate_unit(unit, check=check, materialize=materialize)
        )
    return headers, tuple(components), messages
