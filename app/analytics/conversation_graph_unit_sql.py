"""Persist and reopen conversation-local graph membership units."""

from __future__ import annotations

from datetime import datetime, timezone

from app.analytics.conversation_graph_units import (
    ConversationGraphReference,
    ConversationGraphUnit,
    ConversationGraphUnitHeader,
)
from app.analytics.opaque_refs import account_ref, require_opaque_ref


def supported(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_graph_refs'"
    ).fetchone() is not None


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("conversation_graph_unit_time_invalid")
    return result.astimezone(timezone.utc)


def _optional_instant(value: str | None) -> datetime | None:
    return None if value is None else _instant(value)


def _header(row) -> ConversationGraphUnitHeader:
    return ConversationGraphUnitHeader(
        account_ref=row["creator_account_id"],
        conversation_ref=row["conversation_ref"],
        input_digest=row["input_digest"],
        config_digest=row["config_digest"],
        retention_cutoff=_instant(row["retention_cutoff"]),
        expires_at=_instant(row["expires_at"]),
        participant_ref=require_opaque_ref(row["participant_ref"], "participant"),
        started_at=_optional_instant(row["started_at"]),
        ended_at=_optional_instant(row["ended_at"]),
        graph_digest=row["graph_digest"],
        node_count=int(row["node_count"]),
        edge_count=int(row["edge_count"]),
        unit_id=row["unit_id"],
    )


def load_reference(
    connection,
    generation_id: str,
    account: str,
    conversation: str,
    input_digest: str,
    config_digest: str,
) -> ConversationGraphReference | None:
    row = connection.execute(
        """SELECT r.*,u.graph_digest,u.node_count,u.edge_count
           FROM conversation_graph_refs r
           JOIN conversation_graph_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=?
             AND r.conversation_ref=? AND r.input_digest=? AND r.config_digest=?""",
        (generation_id, account, conversation, input_digest, config_digest),
    ).fetchone()
    return None if row is None else ConversationGraphReference(generation_id, _header(row))


def load_unit(
    connection,
    generation_id: str,
    account: str,
    conversation: str,
) -> ConversationGraphUnit | None:
    row = connection.execute(
        """SELECT r.*,u.graph_digest,u.node_count,u.edge_count,u.node_ids,u.edge_ids
           FROM conversation_graph_refs r
           JOIN conversation_graph_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=? AND r.conversation_ref=?""",
        (generation_id, account, conversation),
    ).fetchone()
    if row is None:
        return None
    return ConversationGraphUnit(_header(row), row["node_ids"], row["edge_ids"])


def list_references(
    connection, generation_id: str, account: str
) -> tuple[ConversationGraphReference, ...]:
    rows = connection.execute(
        """SELECT r.*,u.graph_digest,u.node_count,u.edge_count
           FROM conversation_graph_refs r
           JOIN conversation_graph_units u USING(creator_account_id,unit_id)
           WHERE r.generation_id=? AND r.creator_account_id=?
           ORDER BY r.conversation_ref""",
        (generation_id, account),
    )
    return tuple(ConversationGraphReference(generation_id, _header(row)) for row in rows)


def insert_units(connection, generation_id: str, values, *, check=lambda: None) -> int:
    """Persist optional graph-unit cache rows without weakening publication."""

    if not connection.in_transaction:
        raise ValueError("conversation_graph_units_require_transaction")
    if not supported(connection):
        return 0
    inserted = 0
    for value in values:
        check()
        header = value.header
        if isinstance(value, ConversationGraphReference):
            cursor = connection.execute(
                """INSERT INTO conversation_graph_refs
                   (generation_id,creator_account_id,conversation_ref,input_digest,config_digest,
                    retention_cutoff,expires_at,participant_ref,started_at,ended_at,unit_id)
                   SELECT ?,creator_account_id,conversation_ref,input_digest,config_digest,
                          retention_cutoff,expires_at,participant_ref,started_at,ended_at,unit_id
                   FROM conversation_graph_refs
                   WHERE generation_id=? AND creator_account_id=? AND conversation_ref=?
                     AND input_digest=? AND config_digest=? AND unit_id=?""",
                (
                    generation_id,
                    value.generation_id,
                    header.account_ref,
                    header.conversation_ref,
                    header.input_digest,
                    header.config_digest,
                    header.unit_id,
                ),
            )
            inserted += max(0, cursor.rowcount)
            continue
        if not isinstance(value, ConversationGraphUnit):
            raise TypeError("conversation_graph_unit_invalid")
        row = connection.execute(
            """SELECT graph_digest,node_count,edge_count,node_ids,edge_ids
               FROM conversation_graph_units
               WHERE creator_account_id=? AND unit_id=?""",
            (header.account_ref, header.unit_id),
        ).fetchone()
        exact = (
            row is not None
            and row["graph_digest"] == header.graph_digest
            and int(row["node_count"]) == header.node_count
            and int(row["edge_count"]) == header.edge_count
            and row["node_ids"] == value.node_ids
            and row["edge_ids"] == value.edge_ids
        )
        if row is not None and not exact:
            # Optional cache corruption cannot overwrite immutable predecessor data.
            continue
        if row is None:
            connection.execute(
                """INSERT INTO conversation_graph_units
                   (creator_account_id,unit_id,graph_digest,node_count,edge_count,node_ids,edge_ids)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    header.account_ref,
                    header.unit_id,
                    header.graph_digest,
                    header.node_count,
                    header.edge_count,
                    value.node_ids,
                    value.edge_ids,
                ),
            )
        connection.execute(
            """INSERT INTO conversation_graph_refs
               (generation_id,creator_account_id,conversation_ref,input_digest,config_digest,
                retention_cutoff,expires_at,participant_ref,started_at,ended_at,unit_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                header.account_ref,
                header.conversation_ref,
                header.input_digest,
                header.config_digest,
                header.retention_cutoff.isoformat(),
                header.expires_at.isoformat(),
                header.participant_ref,
                None if header.started_at is None else header.started_at.isoformat(),
                None if header.ended_at is None else header.ended_at.isoformat(),
                header.unit_id,
            ),
        )
        inserted += 1
    check()
    return inserted


def active_reader(store, account_id: str):
    """Return the witnessed active generation and graph-unit helpers."""

    partition = account_ref(account_id)
    connection = store.database.connect()
    generation = connection.execute(
        """SELECT * FROM projection_generations
           WHERE creator_account_id=? AND status='active' AND activated_at IS NOT NULL""",
        (partition,),
    ).fetchone()
    intent = None if generation is None else store.activation.get(generation["generation_id"])
    valid = (
        generation is not None
        and store._intent_matches(generation, intent, require_completed=True)
        and intent.creator_account_id == account_id
        and supported(connection)
    )
    return connection, generation if valid else None
