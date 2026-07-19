"""Closed, domain-separated opaque references for the derived analytics plane."""

from __future__ import annotations

import hashlib
import re
from typing import Literal, NewType


RefDomain = Literal[
    "account",
    "conversation",
    "participant",
    "message",
    "topic",
    "entity",
    "graph_node",
    "graph_edge",
]
AccountPartitionRef = NewType("AccountPartitionRef", str)

REF_PREFIXES: dict[RefDomain, str] = {
    "account": "a1",
    "conversation": "c1",
    "participant": "p1",
    "message": "m1",
    "topic": "t1",
    "entity": "x1",
    "graph_node": "g1",
    "graph_edge": "e1",
}
REF_PATTERN = re.compile(r"^(?:a1|c1|p1|m1|t1|x1|g1|e1):[0-9a-f]{64}$")


def opaque_ref(domain: RefDomain, *identity_parts: str) -> str:
    """Return the sole analytics reference format without embedding source values."""

    if domain not in REF_PREFIXES or not identity_parts:
        raise ValueError("analytics_ref_input_invalid")
    digest = hashlib.sha256()
    digest.update(b"ofca:analytics-ref:v1\0")
    domain_bytes = domain.encode("ascii")
    digest.update(len(domain_bytes).to_bytes(2, "big"))
    digest.update(domain_bytes)
    for part in identity_parts:
        if not isinstance(part, str) or not part:
            raise ValueError("analytics_ref_input_invalid")
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{REF_PREFIXES[domain]}:{digest.hexdigest()}"


def require_opaque_ref(value: str, domain: RefDomain | None = None) -> str:
    """Validate an opaque reference using a non-disclosing fixed error."""

    if not isinstance(value, str) or REF_PATTERN.fullmatch(value) is None:
        raise ValueError("analytics_ref_invalid")
    if domain is not None and not value.startswith(f"{REF_PREFIXES[domain]}:"):
        raise ValueError("analytics_ref_domain_invalid")
    return value


def account_ref(creator_account_id: str) -> str:
    """Hash a canonical account identifier into its analytics partition."""

    return opaque_ref("account", creator_account_id)


def normalize_account_ref(creator_account_id: str) -> str:
    """Map a canonical account identifier to its opaque derived partition."""

    return account_ref(creator_account_id)


def validated_account_ref(value: str) -> AccountPartitionRef:
    """Admit a precomputed account partition only at explicit internal seams."""

    return AccountPartitionRef(require_opaque_ref(value, "account"))


def conversation_ref(creator_account_id: str, conversation_id: str) -> str:
    return opaque_ref("conversation", creator_account_id, conversation_id)


def participant_ref(creator_account_id: str, participant_id: str) -> str:
    return opaque_ref("participant", creator_account_id, participant_id)


def message_ref(
    creator_account_id: str, conversation_id: str, message_id: str
) -> str:
    return opaque_ref("message", creator_account_id, conversation_id, message_id)


def topic_ref(creator_account_id: str, taxonomy_id: str) -> str:
    return opaque_ref("topic", creator_account_id, taxonomy_id)


def entity_ref(
    creator_account_id: str, entity_type: str, normalized_value: str
) -> str:
    return opaque_ref("entity", creator_account_id, entity_type, normalized_value)


def graph_node_ref(account_partition_ref: str, kind: str, identity_ref: str) -> str:
    require_opaque_ref(account_partition_ref, "account")
    return opaque_ref("graph_node", account_partition_ref, kind, identity_ref)


def graph_edge_ref(
    account_partition_ref: str,
    relation: str,
    source_ref: str,
    target_ref: str,
    qualifier: str,
) -> str:
    require_opaque_ref(account_partition_ref, "account")
    return opaque_ref(
        "graph_edge",
        account_partition_ref,
        relation,
        source_ref,
        target_ref,
        qualifier or "none",
    )
