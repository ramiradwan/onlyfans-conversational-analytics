"""Opaque identities for the disposable analytics graph."""

from __future__ import annotations

from app.analytics.opaque_refs import (
    AccountPartitionRef,
    graph_edge_ref,
    graph_node_ref,
    require_opaque_ref,
    validated_account_ref,
)


def graph_id(
    partition_ref: AccountPartitionRef, kind: str, *identity_parts: str
) -> str:
    """Create a graph identifier inside an explicitly validated partition."""

    if not partition_ref or not kind or not identity_parts:
        raise ValueError("graph_id_input_invalid")
    account_partition_ref = validated_account_ref(partition_ref)
    if kind == "edge":
        if len(identity_parts) != 4:
            raise ValueError("graph_id_input_invalid")
        return graph_edge_ref(account_partition_ref, *identity_parts)
    return graph_node_ref(account_partition_ref, kind, "\0".join(identity_parts))


def require_graph_id(value: str, *, expected_kind: str | None = None) -> str:
    """Validate the closed identifier grammar using a non-disclosing error code."""

    domain = "graph_edge" if expected_kind == "edge" else "graph_node"
    try:
        require_opaque_ref(value, domain)
    except ValueError:
        raise ValueError("graph_id_invalid")
    return value
