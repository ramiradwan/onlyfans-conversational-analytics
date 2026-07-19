"""Closed property schemas for privacy-preserving graph persistence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Callable


_ENTITY_REF = re.compile(r"^x1:[0-9a-f]{64}$")
_TOPICS = {
    "feedback": "Feedback",
    "greeting": "Greeting",
    "media": "Media",
    "pricing": "Pricing",
    "scheduling": "Scheduling",
    "support": "Support",
}


def _number(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounded_number(minimum: float, maximum: float) -> Callable[[object], bool]:
    return lambda value: _number(value) and (
        value is None or minimum <= float(value) <= maximum
    )


def _nonnegative(value: object) -> bool:
    return _number(value) and (value is None or float(value) >= 0.0)


def _choice(*values: str) -> Callable[[object], bool]:
    allowed = frozenset(values)
    return lambda value: isinstance(value, str) and value in allowed


NODE_PROPERTY_RULES: dict[str, dict[str, Callable[[object], bool]]] = {
    "participant": {"role": _choice("creator", "counterpart")},
    "conversation": {
        "message_count": _integer,
        "turn_count": _integer,
        "average_sentiment_score": _bounded_number(-1.0, 1.0),
        "response_coverage": _bounded_number(0.0, 1.0),
    },
    "message": {
        "direction": _choice("inbound", "outbound"),
        "source_ordinal": _integer,
        "character_count": _integer,
    },
    "topic": {
        "taxonomy_id": lambda value: isinstance(value, str) and value in _TOPICS,
        "label": lambda value: isinstance(value, str) and value in _TOPICS.values(),
    },
    "entity": {
        "entity_type": _choice("amount", "hashtag", "mention", "url"),
        "entity_ref": lambda value: isinstance(value, str)
        and bool(_ENTITY_REF.fullmatch(value)),
    },
    "affect_state": {
        "label": _choice("positive", "neutral", "negative"),
        "score": _bounded_number(-1.0, 1.0),
        "confidence": _bounded_number(0.0, 1.0),
    },
    "engagement_state": {
        "state": _choice(
            "acknowledgement",
            "commitment",
            "constraint",
            "coordination",
            "information",
            "inquiry",
            "minimal",
            "transactional",
        ),
        "confidence": _bounded_number(0.0, 1.0),
    },
}

EDGE_PROPERTY_RULES: dict[str, dict[str, Callable[[object], bool]]] = {
    "participates_in": {"role": _choice("creator", "counterpart")},
    "contains": {},
    "sent": {},
    "received_by": {},
    "expresses_affect": {},
    "has_engagement_state": {},
    "mentions_topic": {"confidence": _bounded_number(0.0, 1.0)},
    "mentions_entity": {"confidence": _bounded_number(0.0, 1.0)},
    "precedes": {
        "scope": _choice("message", "conversation"),
        "interval_seconds": _nonnegative,
    },
}


def _validate(
    properties: Mapping[str, Any], rules: Mapping[str, Callable[[object], bool]]
) -> dict[str, Any]:
    if set(properties) - set(rules):
        raise ValueError("graph_property_unknown")
    for key, value in properties.items():
        if isinstance(value, (Mapping, list, tuple, set)) or not rules[key](value):
            raise ValueError("graph_property_invalid")
    return {key: properties[key] for key in sorted(properties)}


def validate_node_properties(kind: str, properties: Mapping[str, Any]) -> dict[str, Any]:
    try:
        validated = _validate(properties, NODE_PROPERTY_RULES[kind])
    except KeyError as error:
        raise ValueError("graph_kind_invalid") from error
    if kind == "topic" and validated:
        taxonomy_id = validated.get("taxonomy_id")
        if not isinstance(taxonomy_id, str) or validated.get("label") != _TOPICS.get(
            taxonomy_id
        ):
            raise ValueError("graph_property_invalid")
    return validated


def validate_edge_properties(
    relation: str, properties: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        return _validate(properties, EDGE_PROPERTY_RULES[relation])
    except KeyError as error:
        raise ValueError("graph_relation_invalid") from error
