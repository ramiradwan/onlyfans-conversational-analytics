"""Independent semantic oracle for analytics rebuilds."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from statistics import median
from typing import Any

from app.analytics.historical_derivation import (
    HISTORICAL_DERIVATION_SCHEMA,
    PARTICIPANT_ANALYTICS_MAX_DAYS,
    RETENTION_BASIS,
)
from app.models.analytics import RebuildArtifact
from app.canonical.read_models import AccountReadModel


class AnalyticsOracleMismatch(AssertionError):
    """A directly classified analytics semantic or provenance mismatch."""


@dataclass(frozen=True, slots=True)
class ReproducibilityContext:
    """Closed context consumed or witnessed by a deterministic rebuild."""

    canonical_account_ref: str
    canonical_view_revision: int
    canonical_content_digest: str
    pipeline_revision: str
    pipeline_config_digest: str
    analyzer_provenance: tuple[tuple[str, str, str], ...]
    graph_schema_version: str
    retention_policy: tuple[int, str, str]
    evaluation_clock: datetime
    deterministic_seed: int | None = None
    external_enrichment_provenance: tuple[()] = ()

    def json_safe(self) -> dict[str, Any]:
        document = asdict(self)
        document["evaluation_clock"] = self.evaluation_clock.astimezone(timezone.utc).isoformat()
        return document


def context_for(
    creator_account_id: str,
    account: AccountReadModel,
    *,
    pipeline_revision: str,
    pipeline_config_digest: str,
    analyzer_provenance: tuple[tuple[str, str, str], ...],
    evaluation_clock: datetime,
    deterministic_seed: int | None,
) -> ReproducibilityContext:
    """Record every contract input for one frozen clean rebuild."""

    if evaluation_clock.tzinfo is None or evaluation_clock.utcoffset() is None:
        raise ValueError("evaluation_clock must be timezone-aware")
    canonical_content_digest = _canonical_content_digest(account)
    return ReproducibilityContext(
        canonical_account_ref=_opaque_ref("account", creator_account_id),
        canonical_view_revision=account.view_revision,
        canonical_content_digest=canonical_content_digest,
        pipeline_revision=pipeline_revision,
        pipeline_config_digest=pipeline_config_digest,
        analyzer_provenance=tuple(sorted(analyzer_provenance)),
        graph_schema_version="relationship_graph.v1",
        retention_policy=(
            PARTICIPANT_ANALYTICS_MAX_DAYS,
            RETENTION_BASIS,
            HISTORICAL_DERIVATION_SCHEMA,
        ),
        evaluation_clock=evaluation_clock.astimezone(timezone.utc),
        deterministic_seed=deterministic_seed,
    )


def _canonical_content_digest(account: AccountReadModel) -> str:
    """Independent serialization of the documented canonical witness format."""

    encoded = json.dumps(
        {"view_revision": account.view_revision, "conversations": account.conversations},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"ofca:canonical-account:v1\0" + encoded).hexdigest()


def _opaque_ref(domain: str, *identity_parts: str) -> str:
    """Independent domain-separated opaque-reference calculation for the oracle."""

    prefixes = {
        "account": "a1",
        "conversation": "c1",
        "participant": "p1",
        "message": "m1",
        "topic": "t1",
        "entity": "x1",
        "graph_node": "g1",
        "graph_edge": "e1",
    }
    digest = hashlib.sha256(b"ofca:analytics-ref:v1\0")
    encoded_domain = domain.encode("ascii")
    digest.update(len(encoded_domain).to_bytes(2, "big"))
    digest.update(encoded_domain)
    for part in identity_parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefixes[domain]}:{digest.hexdigest()}"


def _model(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _sorted_models(values: list[Any], *keys: str) -> list[dict[str, Any]]:
    return [_model(value) for value in sorted(values, key=lambda value: tuple(getattr(value, key) for key in keys))]


def normalize_artifact(artifact: RebuildArtifact) -> dict[str, Any]:
    """Return semantic fields without projection lifecycle values."""

    projection = artifact.projection
    projection_document = _model(projection)
    projection_document.pop("projection_generation", None)
    projection_document.pop("projection_digest", None)
    return {
        "canonical_witness": {
            "account_ref": projection.account_ref,
            "source_revision": projection.source_revision,
            "canonical_content_digest": projection.canonical_content_digest,
        },
        "pipeline_provenance": {
            "schema_version": projection.schema_version,
            "pipeline_revision": projection.pipeline_revision,
            "pipeline_config_digest": projection.pipeline_config_digest,
            "pipeline_identity_digest": projection.pipeline_identity_digest,
            "analyzers": _sorted_models(projection.analyzers, "analyzer_name"),
        },
        "semantic_projection": {
            "availability": projection.availability.value,
            "window": _model(projection.window),
            "message_enrichments": _sorted_models(
                projection.message_enrichments, "conversation_ref", "source_ordinal"
            ),
            "conversation_metrics": _sorted_models(
                projection.conversation_metrics, "conversation_ref"
            ),
            "creator_metrics": _model(projection.creator_metrics),
            "graph": _model(projection.graph),
            "graph_digest": projection.graph_digest,
            # Fresh stores share a generation; convergence removes this field.
            "projection_digest": projection.projection_digest,
            "projection_without_lifecycle": projection_document,
        },
        "graph": {
            "nodes": _sorted_models(artifact.nodes, "node_id"),
            "edges": _sorted_models(artifact.edges, "edge_id"),
        },
    }


def _assert_context(artifact: RebuildArtifact, context: ReproducibilityContext) -> None:
    projection = artifact.projection
    actual_analyzers = tuple(
        sorted(
            (item.analyzer_name, item.revision, item.config_digest)
            for item in projection.analyzers
        )
    )
    expected = {
        "account_ref": context.canonical_account_ref,
        "source_revision": context.canonical_view_revision,
        "canonical_content_digest": context.canonical_content_digest,
        "pipeline_revision": context.pipeline_revision,
        "pipeline_config_digest": context.pipeline_config_digest,
        "analyzer_provenance": context.analyzer_provenance,
    }
    observed = {
        "account_ref": projection.account_ref,
        "source_revision": projection.source_revision,
        "canonical_content_digest": projection.canonical_content_digest,
        "pipeline_revision": projection.pipeline_revision,
        "pipeline_config_digest": projection.pipeline_config_digest,
        "analyzer_provenance": actual_analyzers,
    }
    if observed != expected:
        raise AnalyticsOracleMismatch(
            f"reproducibility context mismatch: expected={expected!r}, observed={observed!r}"
        )


def _assert_referential_closure(normalized: dict[str, Any]) -> None:
    node_ids = {node["node_id"] for node in normalized["graph"]["nodes"]}
    dangling = [
        edge["edge_id"]
        for edge in normalized["graph"]["edges"]
        if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids
    ]
    if dangling:
        raise AnalyticsOracleMismatch(f"derived referential closure failed for edges {dangling!r}")


def _assert_graph_digest(artifact: RebuildArtifact) -> None:
    """Independently bind the graph digest to normalized public node/edge rows."""

    value = {
        "nodes": [_model(node) for node in sorted(artifact.nodes, key=lambda item: item.node_id)],
        "edges": [_model(edge) for edge in sorted(artifact.edges, key=lambda item: item.edge_id)],
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if artifact.projection.graph_digest != expected:
        raise AnalyticsOracleMismatch(
            f"graph digest mismatch: expected={expected!r}, observed={artifact.projection.graph_digest!r}"
        )


def expected_active_refs(
    creator_account_id: str, account: AccountReadModel, context: ReproducibilityContext
) -> dict[str, set[str]]:
    """Derive active canonical identity expectations without production helpers."""

    cutoff = context.evaluation_clock - timedelta(days=context.retention_policy[0])
    conversations: set[str] = set()
    participants: set[str] = set()
    messages: set[str] = set()
    for conversation_id, conversation in account.conversations.items():
        active_messages = [
            message
            for message in conversation["messages"]
            if datetime.fromisoformat(str(message["sent_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            > cutoff
        ]
        if not active_messages:
            continue
        participant_id = str(conversation["platform_user_id"])
        conversations.add(_opaque_ref("conversation", creator_account_id, conversation_id))
        participants.add(_opaque_ref("participant", creator_account_id, participant_id))
        messages.update(
            _opaque_ref("message", creator_account_id, conversation_id, str(message["message_id"]))
            for message in active_messages
        )
    return {"conversations": conversations, "participants": participants, "messages": messages}


# Keep oracle vocabulary independent of production analytics helpers.
_TOKEN_RE = re.compile(r"[\w']+", flags=re.UNICODE)
_POSITIVE_TERMS = frozenset(
    {
        "appreciate", "excellent", "glad", "good", "great", "happy", "helpful",
        "resolved", "thanks", "thank", "welcome", "yes",
    }
)
_NEGATIVE_TERMS = frozenset(
    {
        "cancel", "confused", "delay", "delayed", "error", "issue", "problem",
        "sorry", "unhappy", "upset", "wrong",
    }
)
_NEGATORS = frozenset({"hardly", "never", "no", "not"})
_TOPICS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("feedback", "Feedback", frozenset({"appreciate", "feedback", "great", "helpful", "thanks"})),
    ("greeting", "Greeting", frozenset({"hello", "hey", "hi", "welcome"})),
    ("media", "Media", frozenset({"file", "image", "link", "media", "photo", "upload", "video"})),
    ("pricing", "Pricing", frozenset({"budget", "cost", "payment", "price", "tip"})),
    ("scheduling", "Scheduling", frozenset({"available", "calendar", "schedule", "time", "today", "tomorrow"})),
    ("support", "Support", frozenset({"error", "help", "issue", "problem", "resolve", "support"})),
)
_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)),
    ("mention", re.compile(r"(?<!\w)@[A-Za-z0-9_]+")),
    ("hashtag", re.compile(r"(?<!\w)#[A-Za-z0-9_]+")),
    ("amount", re.compile(r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|gbp))", flags=re.IGNORECASE)),
)
_ENGAGEMENT_SIGNALS: tuple[tuple[str, frozenset[str]], ...] = (
    ("constraint", frozenset({"cannot", "can't", "limit", "restricted", "unavailable"})),
    ("transactional", frozenset({"budget", "cost", "payment", "price", "tip"})),
    ("coordination", frozenset({"available", "calendar", "schedule", "time", "tomorrow"})),
    ("acknowledgement", frozenset({"appreciate", "got", "okay", "thanks", "thank"})),
    ("commitment", frozenset({"confirm", "send", "will"})),
)
_QUESTION_TERMS = frozenset({"can", "could", "how", "what", "when", "where", "who", "why", "would"})
_AMOUNT_RE = re.compile(
    r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|gbp))",
    flags=re.IGNORECASE,
)


def _utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticsOracleMismatch("canonical timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_config_digest(name: str, revision: str, config: Any) -> str:
    encoded = json.dumps(
        {"name": name, "revision": revision, "config": config},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _analyzer_descriptor(
    context: ReproducibilityContext, name: str
) -> tuple[str, str, str]:
    for descriptor in context.analyzer_provenance:
        if descriptor[0] == name:
            return descriptor
    raise AnalyticsOracleMismatch(f"missing analyzer in reproducibility context: {name}")


def _sentiment(text: str, descriptor: tuple[str, str, str]) -> dict[str, Any]:
    tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
    total = 0
    hits = 0
    for index, token in enumerate(tokens):
        polarity = 1 if token in _POSITIVE_TERMS else -1 if token in _NEGATIVE_TERMS else 0
        if not polarity:
            continue
        if index > 0 and tokens[index - 1] in _NEGATORS:
            polarity *= -1
        total += polarity
        hits += 1
    score = 0.0 if not hits else round(total / hits, 6)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    confidence = 0.35 if not hits else min(0.95, 0.45 + hits * 0.1)
    return {
        "label": label,
        "score": score,
        "confidence": round(confidence, 6),
        "evidence_count": hits,
        "analyzer_name": descriptor[0],
        "analyzer_revision": descriptor[1],
        "analyzer_config_digest": descriptor[2],
        "analysis_mode": "baseline",
        "calibration_status": "not_calibrated",
    }


def _topic_entities(
    creator_account_id: str, text: str, descriptor: tuple[str, str, str]
) -> dict[str, Any]:
    token_set = {token.lower() for token in _TOKEN_RE.findall(text)}
    topics = []
    for taxonomy_id, label, terms in _TOPICS:
        evidence = sorted(token_set.intersection(terms))
        if evidence:
            topics.append(
                {
                    "topic_ref": _opaque_ref("topic", creator_account_id, taxonomy_id),
                    "taxonomy_id": taxonomy_id,
                    "label": label,
                    "confidence": round(min(0.95, 0.55 + 0.1 * len(evidence)), 6),
                    "evidence_count": len(evidence),
                }
            )
    entities: list[tuple[int, int, str, dict[str, Any]]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for entity_type, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if entity_type == "url":
                value = value.rstrip(".,;:!?)]}\"")
            end_offset = match.start() + len(value)
            normalized = value.casefold().replace(" ", "")
            identity = (entity_type, match.start(), end_offset, normalized)
            if identity in seen:
                continue
            seen.add(identity)
            entities.append(
                (
                    match.start(),
                    end_offset,
                    normalized,
                    {
                        "entity_ref": _opaque_ref("entity", creator_account_id, entity_type, normalized),
                        "entity_type": entity_type,
                        "confidence": 1.0,
                    },
                )
            )
    entities.sort(key=lambda item: (item[0], item[1], item[3]["entity_type"], item[2]))
    return {
        "topics": topics,
        "entities": [item[3] for item in entities],
        "analyzer_name": descriptor[0],
        "analyzer_revision": descriptor[1],
        "analyzer_config_digest": descriptor[2],
        "analysis_mode": "baseline",
        "calibration_status": "not_calibrated",
    }


def _engagement(text: str, descriptor: tuple[str, str, str]) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        state, confidence, signal_count = "minimal", 1.0, 1
    else:
        tokens = [token.lower() for token in _TOKEN_RE.findall(stripped)]
        token_set = set(tokens)
        if _AMOUNT_RE.search(stripped):
            state, confidence, signal_count = "transactional", 0.85, 1
        else:
            state = ""
            confidence = 0.0
            signal_count = 0
            for candidate, terms in _ENGAGEMENT_SIGNALS:
                signals = sorted(token_set.intersection(terms))
                if signals:
                    state = candidate
                    confidence = round(min(0.95, 0.65 + 0.05 * len(signals)), 6)
                    signal_count = len(signals)
                    break
            if not state:
                question_signals = sorted(token_set.intersection(_QUESTION_TERMS))
                if "?" in stripped or (tokens and tokens[0] in _QUESTION_TERMS):
                    signals = (["question_mark"] if "?" in stripped else []) + question_signals
                    state, confidence, signal_count = "inquiry", 0.85, len(dict.fromkeys(signals))
                else:
                    state, confidence, signal_count = "information", 0.55, 1
    return {
        "state": state,
        "confidence": confidence,
        "signal_count": signal_count,
        "analyzer_name": descriptor[0],
        "analyzer_revision": descriptor[1],
        "analyzer_config_digest": descriptor[2],
        "analysis_mode": "baseline",
        "calibration_status": "not_calibrated",
    }


def _canonical_active_conversations(
    creator_account_id: str,
    account: AccountReadModel,
    context: ReproducibilityContext,
) -> list[dict[str, Any]]:
    """Normalize final canonical rows with the contract's exclusive cutoff."""

    cutoff = context.evaluation_clock - timedelta(days=context.retention_policy[0])
    result = []
    for conversation_id in sorted(account.conversations):
        raw = account.conversations[conversation_id]
        messages = [
            {
                "message_id": str(message["message_id"]),
                "source_ordinal": int(message["source_ordinal"]),
                "text": str(message["text"]),
                "sent_at": _utc(str(message["sent_at"])),
                "direction": str(message["direction"]),
            }
            for message in raw["messages"]
        ]
        messages.sort(key=lambda item: (item["sent_at"], item["source_ordinal"]))
        active = [item for item in messages if item["sent_at"] > cutoff]
        if not active:
            continue
        participant_id = str(raw["platform_user_id"])
        result.append(
            {
                "conversation_id": conversation_id,
                "participant_id": participant_id,
                "unread_count": int(raw.get("unread_count", 0)),
                "conversation_ref": _opaque_ref("conversation", creator_account_id, conversation_id),
                "participant_ref": _opaque_ref("participant", creator_account_id, participant_id),
                "messages": active,
            }
        )
    return result


def _expected_enrichments(
    creator_account_id: str,
    conversations: list[dict[str, Any]],
    context: ReproducibilityContext,
) -> list[dict[str, Any]]:
    sentiment_descriptor = _analyzer_descriptor(context, "rule_based_sentiment")
    topic_descriptor = _analyzer_descriptor(context, "rule_based_topics_entities")
    engagement_descriptor = _analyzer_descriptor(context, "rule_based_engagement")
    result: list[dict[str, Any]] = []
    account = _opaque_ref("account", creator_account_id)
    for conversation in conversations:
        for message in conversation["messages"]:
            result.append(
                {
                    "account_ref": account,
                    "conversation_ref": conversation["conversation_ref"],
                    "participant_ref": conversation["participant_ref"],
                    "message_ref": _opaque_ref(
                        "message",
                        creator_account_id,
                        conversation["conversation_id"],
                        message["message_id"],
                    ),
                    "source_ordinal": message["source_ordinal"],
                    "sent_at": _time(message["sent_at"]),
                    "direction": message["direction"],
                    "sentiment": _sentiment(message["text"], sentiment_descriptor),
                    "topic_entities": _topic_entities(
                        creator_account_id, message["text"], topic_descriptor
                    ),
                    "engagement": _engagement(message["text"], engagement_descriptor),
                }
            )
    return sorted(
        result, key=lambda item: (item["conversation_ref"], item["source_ordinal"])
    )


def _ordered_counts(values: Counter[str]) -> dict[str, int]:
    return {key: values[key] for key in sorted(values)}


def _metric_provenance(metric_name: str, sample_count: int) -> dict[str, Any]:
    if metric_name == "conversation_metrics":
        revision = "conversation_metrics.baseline.v2"
        config = {
            "response_pairing": "latest_inbound_then_first_outbound",
            "turn_boundary": "direction_change",
            "silence": "adjacent_message_gap",
        }
    elif metric_name == "creator_metrics":
        revision = "creator_metrics.baseline.v2"
        config = {
            "aggregation": "message_weighted_conversation_metrics",
            "participant_identity": "distinct_platform_user_id",
            "response_coverage": "responded_over_opportunities",
        }
    else:
        raise AnalyticsOracleMismatch(f"unknown metric provenance {metric_name}")
    return {
        "metric_name": metric_name,
        "revision": revision,
        "config_digest": _stable_config_digest(metric_name, revision, config),
        "mode": "baseline",
        "calibration_status": "not_calibrated",
        "sample_count": sample_count,
        "sample_coverage": 1.0 if sample_count else None,
        "unavailable_reason": None if sample_count else "no_messages",
    }


def _conversation_metrics(
    account_ref: str,
    conversation: dict[str, Any],
    enrichments: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        enrichments,
        key=lambda item: (_utc(item["sent_at"]), item["source_ordinal"]),
    )
    inbound = sum(item["direction"] == "inbound" for item in ordered)
    outbound = len(ordered) - inbound
    turns = 0
    previous_direction: str | None = None
    opportunities = 0
    response_seconds: list[float] = []
    pending_inbound_at: datetime | None = None
    for item in ordered:
        if item["direction"] != previous_direction:
            turns += 1
            if item["direction"] == "inbound":
                opportunities += 1
        at = _utc(item["sent_at"])
        if item["direction"] == "inbound":
            pending_inbound_at = at
        elif pending_inbound_at is not None:
            response_seconds.append(max(0.0, (at - pending_inbound_at).total_seconds()))
            pending_inbound_at = None
        previous_direction = item["direction"]
    silence_seconds = [
        max(0.0, (_utc(right["sent_at"]) - _utc(left["sent_at"])).total_seconds())
        for left, right in zip(ordered, ordered[1:])
    ]
    sentiments: Counter[str] = Counter(item["sentiment"]["label"] for item in ordered)
    topics: Counter[str] = Counter()
    entities: Counter[str] = Counter()
    engagements: Counter[str] = Counter()
    for item in ordered:
        topics.update(topic["taxonomy_id"] for topic in item["topic_entities"]["topics"])
        entities.update(entity["entity_type"] for entity in item["topic_entities"]["entities"])
        engagements[item["engagement"]["state"]] += 1
    started = _utc(ordered[0]["sent_at"]) if ordered else None
    ended = _utc(ordered[-1]["sent_at"]) if ordered else None
    duration = 0.0 if started is None or ended is None else max(0.0, (ended - started).total_seconds())
    reasons: dict[str, str] = {}
    if not opportunities:
        reasons["response_coverage"] = "no_response_opportunities"
    if not response_seconds:
        reasons["response_time"] = "no_responses"
    if not ordered:
        reasons["average_sentiment_score"] = "no_messages"
    if not silence_seconds:
        reasons["maximum_silence_seconds"] = "insufficient_messages"
    average_sentiment = sum(item["sentiment"]["score"] for item in ordered) / len(ordered) if ordered else None
    return {
        "account_ref": account_ref,
        "conversation_ref": conversation["conversation_ref"],
        "participant_ref": conversation["participant_ref"],
        "unread_count": conversation["unread_count"],
        "started_at": _time(started),
        "ended_at": _time(ended),
        "duration_seconds": round(duration, 6),
        "message_count": len(ordered),
        "inbound_message_count": inbound,
        "outbound_message_count": outbound,
        "turn_count": turns,
        "response_opportunity_count": opportunities,
        "responded_count": len(response_seconds),
        "response_coverage": round(len(response_seconds) / opportunities, 6) if opportunities else None,
        "average_response_seconds": round(sum(response_seconds) / len(response_seconds), 6) if response_seconds else None,
        "median_response_seconds": round(float(median(response_seconds)), 6) if response_seconds else None,
        "maximum_silence_seconds": round(max(silence_seconds), 6) if silence_seconds else None,
        "average_sentiment_score": round(average_sentiment, 6) if average_sentiment is not None else None,
        "sentiment_counts": _ordered_counts(sentiments),
        "topic_counts": _ordered_counts(topics),
        "entity_counts": _ordered_counts(entities),
        "engagement_counts": _ordered_counts(engagements),
        "provenance": _metric_provenance("conversation_metrics", len(ordered)),
        "window": {"scope": "all_time", "start": _time(started), "end": _time(ended)},
        "unavailable_reasons": reasons,
    }


def _creator_metrics(account_ref: str, conversations: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(conversations, key=lambda item: item["conversation_ref"])
    message_count = sum(item["message_count"] for item in ordered)
    inbound = sum(item["inbound_message_count"] for item in ordered)
    outbound = sum(item["outbound_message_count"] for item in ordered)
    opportunities = sum(item["response_opportunity_count"] for item in ordered)
    responded = sum(item["responded_count"] for item in ordered)
    response_total = sum((item["average_response_seconds"] or 0.0) * item["responded_count"] for item in ordered)
    sentiment_total = sum((item["average_sentiment_score"] or 0.0) * item["message_count"] for item in ordered)
    sentiments: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    entities: Counter[str] = Counter()
    engagements: Counter[str] = Counter()
    for item in ordered:
        sentiments.update(item["sentiment_counts"])
        topics.update(item["topic_counts"])
        entities.update(item["entity_counts"])
        engagements.update(item["engagement_counts"])
    starts = [_utc(item["started_at"]) for item in ordered if item["started_at"] is not None]
    ends = [_utc(item["ended_at"]) for item in ordered if item["ended_at"] is not None]
    reasons: dict[str, str] = {}
    if not ordered:
        reasons["average_messages_per_conversation"] = "no_conversations"
    if not opportunities:
        reasons["response_coverage"] = "no_response_opportunities"
    if not responded:
        reasons["average_response_seconds"] = "no_responses"
    if not message_count:
        reasons["average_sentiment_score"] = "no_messages"
    active_from = min(starts) if starts else None
    active_until = max(ends) if ends else None
    return {
        "account_ref": account_ref,
        "conversation_count": len(ordered),
        "participant_count": len({item["participant_ref"] for item in ordered}),
        "message_count": message_count,
        "inbound_message_count": inbound,
        "outbound_message_count": outbound,
        "active_from": _time(active_from),
        "active_until": _time(active_until),
        "average_messages_per_conversation": round(message_count / len(ordered), 6) if ordered else None,
        "response_opportunity_count": opportunities,
        "responded_count": responded,
        "response_coverage": round(responded / opportunities, 6) if opportunities else None,
        "average_response_seconds": round(response_total / responded, 6) if responded else None,
        "average_sentiment_score": round(sentiment_total / message_count, 6) if message_count else None,
        "sentiment_counts": _ordered_counts(sentiments),
        "topic_counts": _ordered_counts(topics),
        "entity_counts": _ordered_counts(entities),
        "engagement_counts": _ordered_counts(engagements),
        "provenance": _metric_provenance("creator_metrics", message_count),
        "window": {"scope": "all_time", "start": _time(active_from), "end": _time(active_until)},
        "unavailable_reasons": reasons,
    }


def _node_id(account_ref: str, kind: str, identity_ref: str) -> str:
    return _opaque_ref("graph_node", account_ref, kind, identity_ref)


def _edge_id(
    account_ref: str, relation: str, source_id: str, target_id: str, qualifier: str
) -> str:
    return _opaque_ref(
        "graph_edge", account_ref, relation, source_id, target_id, qualifier or "none"
    )


def _add_edge(
    edges: dict[str, dict[str, Any]],
    account_ref: str,
    relation: str,
    source_id: str,
    target_id: str,
    *,
    qualifier: str,
    occurred_at: datetime | None = None,
    sequence: int | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    edge_id = _edge_id(account_ref, relation, source_id, target_id, qualifier)
    edge = {
        "edge_id": edge_id,
        "account_ref": account_ref,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "occurred_at": _time(occurred_at),
        "sequence": sequence,
        "properties": properties or {},
    }
    existing = edges.get(edge_id)
    if existing is not None and existing != edge:
        raise AnalyticsOracleMismatch("independent graph edge identity collision")
    edges[edge_id] = edge


def _expected_graph(
    account_ref: str,
    source_revision: int,
    conversations: list[dict[str, Any]],
    enrichments: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    creator_node = _node_id(account_ref, "participant", account_ref)
    nodes[creator_node] = {
        "node_id": creator_node,
        "account_ref": account_ref,
        "kind": "participant",
        "occurred_at": None,
        "properties": {"role": "creator"},
    }
    metrics_by_conversation = {item["conversation_ref"]: item for item in metrics}
    enrichments_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for enrichment in enrichments:
        enrichments_by_conversation[enrichment["conversation_ref"]].append(enrichment)
    by_participant: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for conversation in sorted(conversations, key=lambda item: item["conversation_id"]):
        metric = metrics_by_conversation[conversation["conversation_ref"]]
        by_participant[conversation["participant_ref"]].append((conversation, metric))
        participant_node = _node_id(account_ref, "participant", conversation["participant_ref"])
        nodes.setdefault(
            participant_node,
            {
                "node_id": participant_node,
                "account_ref": account_ref,
                "kind": "participant",
                "occurred_at": None,
                "properties": {"role": "counterpart"},
            },
        )
        conversation_node = _node_id(account_ref, "conversation", conversation["conversation_ref"])
        nodes[conversation_node] = {
            "node_id": conversation_node,
            "account_ref": account_ref,
            "kind": "conversation",
            "occurred_at": metric["started_at"],
            "properties": {
                "message_count": metric["message_count"],
                "turn_count": metric["turn_count"],
                "average_sentiment_score": metric["average_sentiment_score"],
                "response_coverage": metric["response_coverage"],
            },
        }
        _add_edge(edges, account_ref, "participates_in", creator_node, conversation_node, qualifier="creator", properties={"role": "creator"})
        _add_edge(edges, account_ref, "participates_in", participant_node, conversation_node, qualifier="counterpart", properties={"role": "counterpart"})
        raw_by_ordinal = {
            message["source_ordinal"]: message for message in conversation["messages"]
        }
        ordered = sorted(
            enrichments_by_conversation[conversation["conversation_ref"]],
            key=lambda item: (_utc(item["sent_at"]), item["source_ordinal"]),
        )
        previous_node: str | None = None
        previous_at: datetime | None = None
        for sequence, enrichment in enumerate(ordered):
            raw = raw_by_ordinal[enrichment["source_ordinal"]]
            sent_at = _utc(enrichment["sent_at"])
            message_node = _node_id(account_ref, "message", enrichment["message_ref"])
            nodes[message_node] = {
                "node_id": message_node,
                "account_ref": account_ref,
                "kind": "message",
                "occurred_at": enrichment["sent_at"],
                "properties": {
                    "direction": enrichment["direction"],
                    "source_ordinal": enrichment["source_ordinal"],
                    "character_count": len(raw["text"]),
                },
            }
            _add_edge(edges, account_ref, "contains", conversation_node, message_node, qualifier=enrichment["message_ref"], occurred_at=sent_at, sequence=sequence)
            actor = participant_node if enrichment["direction"] == "inbound" else creator_node
            recipient = creator_node if enrichment["direction"] == "inbound" else participant_node
            _add_edge(edges, account_ref, "sent", actor, message_node, qualifier=enrichment["message_ref"], occurred_at=sent_at, sequence=sequence)
            _add_edge(edges, account_ref, "received_by", message_node, recipient, qualifier=enrichment["message_ref"], occurred_at=sent_at, sequence=sequence)
            affect_node = _node_id(account_ref, "affect_state", enrichment["message_ref"])
            nodes[affect_node] = {
                "node_id": affect_node,
                "account_ref": account_ref,
                "kind": "affect_state",
                "occurred_at": enrichment["sent_at"],
                "properties": {
                    "label": enrichment["sentiment"]["label"],
                    "score": enrichment["sentiment"]["score"],
                    "confidence": enrichment["sentiment"]["confidence"],
                },
            }
            _add_edge(edges, account_ref, "expresses_affect", message_node, affect_node, qualifier=enrichment["message_ref"], occurred_at=sent_at, sequence=sequence)
            engagement_node = _node_id(account_ref, "engagement_state", enrichment["message_ref"])
            nodes[engagement_node] = {
                "node_id": engagement_node,
                "account_ref": account_ref,
                "kind": "engagement_state",
                "occurred_at": enrichment["sent_at"],
                "properties": {
                    "state": enrichment["engagement"]["state"],
                    "confidence": enrichment["engagement"]["confidence"],
                },
            }
            _add_edge(edges, account_ref, "has_engagement_state", message_node, engagement_node, qualifier=enrichment["message_ref"], occurred_at=sent_at, sequence=sequence)
            for topic in enrichment["topic_entities"]["topics"]:
                topic_node = _node_id(account_ref, "topic", topic["topic_ref"])
                nodes.setdefault(
                    topic_node,
                    {
                        "node_id": topic_node,
                        "account_ref": account_ref,
                        "kind": "topic",
                        "occurred_at": None,
                        "properties": {
                            "taxonomy_id": topic["taxonomy_id"],
                            "label": topic["label"],
                        },
                    },
                )
                _add_edge(edges, account_ref, "mentions_topic", message_node, topic_node, qualifier=topic["topic_ref"], occurred_at=sent_at, sequence=sequence, properties={"confidence": topic["confidence"]})
            for entity in enrichment["topic_entities"]["entities"]:
                entity_node = _node_id(account_ref, "entity", entity["entity_ref"])
                nodes.setdefault(
                    entity_node,
                    {
                        "node_id": entity_node,
                        "account_ref": account_ref,
                        "kind": "entity",
                        "occurred_at": None,
                        "properties": {
                            "entity_type": entity["entity_type"],
                            "entity_ref": entity["entity_ref"],
                        },
                    },
                )
                _add_edge(edges, account_ref, "mentions_entity", message_node, entity_node, qualifier=entity["entity_ref"], occurred_at=sent_at, sequence=sequence, properties={"confidence": entity["confidence"]})
            if previous_node is not None and previous_at is not None:
                _add_edge(
                    edges,
                    account_ref,
                    "precedes",
                    previous_node,
                    message_node,
                    qualifier="message",
                    occurred_at=sent_at,
                    sequence=sequence - 1,
                    properties={
                        "scope": "message",
                        "interval_seconds": round(max(0.0, (sent_at - previous_at).total_seconds()), 6),
                    },
                )
            previous_node, previous_at = message_node, sent_at

    for participant_ref, items in sorted(by_participant.items()):
        ordered_items = sorted(
            items,
            key=lambda item: (
                item[1]["started_at"] is None,
                item[1]["started_at"],
                item[1]["conversation_ref"],
            ),
        )
        for (left, left_metrics), (right, right_metrics) in zip(ordered_items, ordered_items[1:]):
            left_id = _node_id(account_ref, "conversation", left["conversation_ref"])
            right_id = _node_id(account_ref, "conversation", right["conversation_ref"])
            properties: dict[str, Any] = {"scope": "conversation"}
            if left_metrics["ended_at"] is not None and right_metrics["started_at"] is not None:
                properties["interval_seconds"] = round(max(0.0, (_utc(right_metrics["started_at"]) - _utc(left_metrics["ended_at"])).total_seconds()), 6)
            _add_edge(
                edges,
                account_ref,
                "precedes",
                left_id,
                right_id,
                qualifier=participant_ref,
                occurred_at=(None if right_metrics["started_at"] is None else _utc(right_metrics["started_at"])),
                properties=properties,
            )
    ordered_nodes = [nodes[key] for key in sorted(nodes)]
    ordered_edges = [edges[key] for key in sorted(edges)]
    node_counts = Counter(node["kind"] for node in ordered_nodes)
    edge_counts = Counter(edge["relation"] for edge in ordered_edges)
    summary = {
        "account_ref": account_ref,
        "source_revision": source_revision,
        "node_count": len(ordered_nodes),
        "edge_count": len(ordered_edges),
        "node_counts_by_kind": _ordered_counts(node_counts),
        "edge_counts_by_relation": _ordered_counts(edge_counts),
    }
    return ordered_nodes, ordered_edges, summary


def _graph_digest_documents(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> str:
    encoded = json.dumps(
        {"nodes": sorted(nodes, key=lambda item: item["node_id"]), "edges": sorted(edges, key=lambda item: item["edge_id"])},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _pipeline_identity(context: ReproducibilityContext) -> str:
    analyzers = sorted(
        (name, revision, digest, "baseline", "not_calibrated")
        for name, revision, digest in context.analyzer_provenance
    )
    payload = {
        "schema_version": "3",
        "pipeline_revision": context.pipeline_revision,
        "pipeline_config_digest": context.pipeline_config_digest,
        "analyzers": analyzers,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(
        b"ofca:analytics-pipeline-identity:v1\0" + encoded
    ).hexdigest()


def _expected_analyzers(
    enrichments: list[dict[str, Any]], context: ReproducibilityContext
) -> list[dict[str, Any]]:
    topic_confidences = [
        topic["confidence"]
        for enrichment in enrichments
        for topic in enrichment["topic_entities"]["topics"]
    ]
    confidence_by_name = {
        "rule_based_sentiment": [
            item["sentiment"]["confidence"] for item in enrichments
        ],
        "rule_based_topics_entities": topic_confidences,
        "rule_based_engagement": [
            item["engagement"]["confidence"] for item in enrichments
        ],
    }
    count = len(enrichments)
    result = []
    for name, revision, digest in context.analyzer_provenance:
        confidences = confidence_by_name.get(name)
        if confidences is None:
            raise AnalyticsOracleMismatch(f"unexpected analyzer in context: {name}")
        result.append(
            {
                "analyzer_name": name,
                "revision": revision,
                "config_digest": digest,
                "mode": "baseline",
                "calibration_status": "not_calibrated",
                "analyzed_sample_count": count,
                "eligible_sample_count": count,
                "sample_coverage": 1.0 if count else None,
                "mean_confidence": round(sum(confidences) / len(confidences), 6) if confidences else None,
                "unavailable_reason": None if count else "no_eligible_samples",
            }
        )
    return sorted(result, key=lambda item: item["analyzer_name"])


def expected_semantic_from_canonical(
    creator_account_id: str,
    account: AccountReadModel,
    context: ReproducibilityContext,
) -> dict[str, Any]:
    """Independently derive every semantic projection surface from canonical head."""

    account_ref = _opaque_ref("account", creator_account_id)
    conversations = _canonical_active_conversations(
        creator_account_id, account, context
    )
    enrichments = _expected_enrichments(creator_account_id, conversations, context)
    metrics_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for enrichment in enrichments:
        metrics_by_conversation[enrichment["conversation_ref"]].append(enrichment)
    metrics = [
        _conversation_metrics(
            account_ref,
            conversation,
            metrics_by_conversation[conversation["conversation_ref"]],
        )
        for conversation in conversations
    ]
    metrics.sort(key=lambda item: item["conversation_ref"])
    creator_metrics = _creator_metrics(account_ref, metrics)
    nodes, edges, graph = _expected_graph(
        account_ref, account.view_revision, conversations, enrichments, metrics
    )
    topic_refs = {
        topic["topic_ref"]
        for enrichment in enrichments
        for topic in enrichment["topic_entities"]["topics"]
    }
    entity_refs = {
        entity["entity_ref"]
        for enrichment in enrichments
        for entity in enrichment["topic_entities"]["entities"]
    }
    return {
        "canonical_witness": {
            "account_ref": account_ref,
            "source_revision": account.view_revision,
            "canonical_content_digest": context.canonical_content_digest,
        },
        "pipeline_provenance": {
            "schema_version": "3",
            "pipeline_revision": context.pipeline_revision,
            "pipeline_config_digest": context.pipeline_config_digest,
            "pipeline_identity_digest": _pipeline_identity(context),
            "analyzers": _expected_analyzers(enrichments, context),
        },
        "semantic_projection": {
            "availability": "available",
            "window": creator_metrics["window"],
            "message_enrichments": enrichments,
            "conversation_metrics": metrics,
            "creator_metrics": creator_metrics,
            "graph": graph,
            "graph_digest": _graph_digest_documents(nodes, edges),
            "projection_without_lifecycle": None,
        },
        "graph": {"nodes": nodes, "edges": edges},
        "identity_refs": {
            "account": {account_ref},
            "conversations": {item["conversation_ref"] for item in conversations},
            "participants": {item["participant_ref"] for item in conversations},
            "messages": {item["message_ref"] for item in enrichments},
            "topics": topic_refs,
            "entities": entity_refs,
        },
    }


def assert_artifact_matches_canonical_semantics(
    artifact: RebuildArtifact,
    expected: dict[str, Any],
) -> None:
    """Reject a self-consistent but canonically wrong derived representation."""

    normalized = normalize_convergence_artifact(artifact)
    _assert_referential_closure(normalized)
    _assert_graph_digest(artifact)
    observed_semantic = {
        key: value
        for key, value in normalized["semantic_projection"].items()
        if key != "projection_without_lifecycle"
    }
    expected_semantic = {
        key: value
        for key, value in expected["semantic_projection"].items()
        if key != "projection_without_lifecycle"
    }
    for field, observed, wanted in (
        ("canonical_witness", normalized["canonical_witness"], expected["canonical_witness"]),
        ("pipeline_provenance", normalized["pipeline_provenance"], expected["pipeline_provenance"]),
        ("semantic_projection", observed_semantic, expected_semantic),
        ("graph", normalized["graph"], expected["graph"]),
    ):
        if observed != wanted:
            raise AnalyticsOracleMismatch(
                f"canonical semantic mismatch in {field}: expected={wanted!r}, observed={observed!r}"
            )
    actual_refs = {
        "account": {artifact.projection.account_ref},
        "conversations": {item.conversation_ref for item in artifact.projection.conversation_metrics},
        "participants": {
            item.participant_ref for item in artifact.projection.conversation_metrics
        },
        "messages": {item.message_ref for item in artifact.projection.message_enrichments},
        "topics": {
            topic.topic_ref
            for item in artifact.projection.message_enrichments
            for topic in item.topic_entities.topics
        },
        "entities": {
            entity.entity_ref
            for item in artifact.projection.message_enrichments
            for entity in item.topic_entities.entities
        },
    }
    if actual_refs != expected["identity_refs"]:
        raise AnalyticsOracleMismatch(
            "canonical semantic mismatch in identity_refs: "
            f"expected={expected['identity_refs']!r}, observed={actual_refs!r}"
        )


def _assert_canonical_identity_mapping(
    artifact: RebuildArtifact, expected_refs: dict[str, set[str]]
) -> None:
    projection = artifact.projection
    actual_messages = {item.message_ref for item in projection.message_enrichments}
    actual_conversations = {item.conversation_ref for item in projection.conversation_metrics}
    actual_participants = {item.participant_ref for item in projection.conversation_metrics}
    actual_participants.update(item.participant_ref for item in projection.message_enrichments)
    actual = {
        "conversations": actual_conversations,
        "participants": actual_participants,
        "messages": actual_messages,
    }
    if actual != expected_refs:
        raise AnalyticsOracleMismatch(
            f"canonical opaque identity mapping mismatch: expected={expected_refs!r}, observed={actual!r}"
        )


def assert_deterministic_rebuilds(
    first: RebuildArtifact,
    second: RebuildArtifact,
    context: ReproducibilityContext,
    *,
    expected_refs: dict[str, set[str]] | None = None,
) -> None:
    """Assert the full deterministic-rebuild matrix across two fresh builds."""

    _assert_context(first, context)
    _assert_context(second, context)
    if expected_refs is not None:
        _assert_canonical_identity_mapping(first, expected_refs)
        _assert_canonical_identity_mapping(second, expected_refs)
    left = normalize_artifact(first)
    right = normalize_artifact(second)
    _assert_referential_closure(left)
    _assert_referential_closure(right)
    _assert_graph_digest(first)
    _assert_graph_digest(second)
    for field in ("canonical_witness", "pipeline_provenance", "semantic_projection", "graph"):
        if left[field] != right[field]:
            raise AnalyticsOracleMismatch(
                f"Analytics rebuild mismatch in {field}: left={left[field]!r}, right={right[field]!r}"
            )


def normalize_convergence_artifact(artifact: RebuildArtifact) -> dict[str, Any]:
    """Normalize convergence output without lifecycle fields."""

    normalized = normalize_artifact(artifact)
    normalized["semantic_projection"].pop("projection_digest", None)
    return normalized


def assert_incremental_rebuild_convergence(
    incremental: RebuildArtifact,
    rebuilt: RebuildArtifact,
    context: ReproducibilityContext,
    *,
    expected_refs: dict[str, set[str]] | None = None,
    expected_semantics: dict[str, Any] | None = None,
) -> None:
    """Compare incremental publication with a clean rebuild."""

    _assert_context(incremental, context)
    _assert_context(rebuilt, context)
    if expected_refs is not None:
        _assert_canonical_identity_mapping(incremental, expected_refs)
        _assert_canonical_identity_mapping(rebuilt, expected_refs)
    if expected_semantics is not None:
        assert_artifact_matches_canonical_semantics(incremental, expected_semantics)
        assert_artifact_matches_canonical_semantics(rebuilt, expected_semantics)
    left = normalize_convergence_artifact(incremental)
    right = normalize_convergence_artifact(rebuilt)
    _assert_referential_closure(left)
    _assert_referential_closure(right)
    _assert_graph_digest(incremental)
    _assert_graph_digest(rebuilt)
    for field in ("canonical_witness", "pipeline_provenance", "semantic_projection", "graph"):
        if left[field] != right[field]:
            raise AnalyticsOracleMismatch(
                f"Analytics convergence mismatch in {field}: "
                f"incremental={left[field]!r}, rebuilt={right[field]!r}"
            )


def assert_active_publication_witness(
    *,
    active_projection: Any | None,
    active_nodes: list[Any],
    active_edges: list[Any],
    active_graph_revision: int | None,
    context: ReproducibilityContext,
    expected_semantics: dict[str, Any],
) -> None:
    """Require live active material and its witness to match canonical head."""

    if active_projection is None:
        raise AnalyticsOracleMismatch("active publication is absent")
    expected = {
        "source_revision": context.canonical_view_revision,
        "canonical_content_digest": context.canonical_content_digest,
        "pipeline_revision": context.pipeline_revision,
        "pipeline_config_digest": context.pipeline_config_digest,
        "graph_digest": expected_semantics["semantic_projection"]["graph_digest"],
    }
    observed = {
        "source_revision": active_projection.source_revision,
        "canonical_content_digest": active_projection.canonical_content_digest,
        "pipeline_revision": active_projection.pipeline_revision,
        "pipeline_config_digest": active_projection.pipeline_config_digest,
        "graph_digest": active_projection.graph_digest,
    }
    if observed != expected or active_graph_revision != context.canonical_view_revision:
        raise AnalyticsOracleMismatch(
            "active publication witness mismatch: "
            f"expected={expected!r}, observed={observed!r}, "
            f"active_graph_revision={active_graph_revision!r}"
        )
    # Materialize content to detect stale projections with current metadata.
    assert_artifact_matches_canonical_semantics(
        RebuildArtifact(
            projection=active_projection,
            nodes=active_nodes,
            edges=active_edges,
        ),
        expected_semantics,
    )


def _graph_node_ref(account_ref: str, kind: str, identity_ref: str) -> str:
    """Independent graph-node identity calculation for deletion absence checks."""

    return _opaque_ref("graph_node", account_ref, kind, identity_ref)


def assert_deleted_material_absent(
    artifact: RebuildArtifact,
    *,
    creator_account_id: str,
    expected_semantics: dict[str, Any],
    removed_messages: tuple[tuple[str, str], ...] = (),
    removed_conversations: tuple[str, ...] = (),
    removed_participants: tuple[str, ...] = (),
) -> None:
    """Assert that tombstoned material is absent from derived state."""

    # Compare complete derived content to detect valid but stale rows.
    assert_artifact_matches_canonical_semantics(artifact, expected_semantics)
    account = artifact.projection.account_ref
    message_refs = {
        _opaque_ref("message", creator_account_id, conversation_id, message_id)
        for conversation_id, message_id in removed_messages
    }
    conversation_refs = {
        _opaque_ref("conversation", creator_account_id, conversation_id)
        for conversation_id in removed_conversations
    }
    participant_refs = {
        _opaque_ref("participant", creator_account_id, participant_id)
        for participant_id in removed_participants
    }
    if message_refs & {item.message_ref for item in artifact.projection.message_enrichments}:
        raise AnalyticsOracleMismatch("deleted message remains in enrichment output")
    if conversation_refs & {
        item.conversation_ref for item in artifact.projection.conversation_metrics
    }:
        raise AnalyticsOracleMismatch("deleted conversation remains in metrics output")
    derived_participants = {
        item.participant_ref for item in artifact.projection.conversation_metrics
    }
    derived_participants.update(
        item.participant_ref for item in artifact.projection.message_enrichments
    )
    if participant_refs & derived_participants:
        raise AnalyticsOracleMismatch("deleted participant remains in derived output")

    forbidden_nodes = {
        *(
            _graph_node_ref(account, "message", value)
            for value in message_refs
        ),
        *(
            _graph_node_ref(account, "affect_state", value)
            for value in message_refs
        ),
        *(
            _graph_node_ref(account, "engagement_state", value)
            for value in message_refs
        ),
        *(
            _graph_node_ref(account, "conversation", value)
            for value in conversation_refs
        ),
        *(
            _graph_node_ref(account, "participant", value)
            for value in participant_refs
        ),
    }
    lingering = forbidden_nodes & {node.node_id for node in artifact.nodes}
    if lingering:
        raise AnalyticsOracleMismatch(
            f"deleted material remains in graph nodes {sorted(lingering)!r}"
        )
    expected_node_ids = {
        node["node_id"] for node in expected_semantics["graph"]["nodes"]
    }
    expected_edges = {
        edge["edge_id"] for edge in expected_semantics["graph"]["edges"]
    }
    actual_node_ids = {node.node_id for node in artifact.nodes}
    actual_edges = {edge.edge_id for edge in artifact.edges}
    if actual_node_ids != expected_node_ids or actual_edges != expected_edges:
        raise AnalyticsOracleMismatch(
            "derived deletion closure differs from final canonical graph"
        )


def reproduction_payload(
    *,
    creator_account_id: str,
    account: AccountReadModel,
    context: ReproducibilityContext,
    first: RebuildArtifact,
    second: RebuildArtifact,
) -> dict[str, Any]:
    """JSON-safe permanent-reproducer material, independent of Hypothesis cache."""

    return {
        "creator_account_id": creator_account_id,
        "canonical_input": {
            "view_revision": account.view_revision,
            "conversations": account.conversations,
        },
        "reproducibility_context": context.json_safe(),
        "expected_semantic_provenance_shape": normalize_artifact(first),
        "actual_semantic_provenance_shape": normalize_artifact(second),
    }
