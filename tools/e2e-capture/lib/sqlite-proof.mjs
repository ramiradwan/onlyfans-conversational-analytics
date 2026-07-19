import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import { pythonExecutable } from './paths.mjs';

const execFileAsync = promisify(execFile);

const PROOF_SCRIPT = String.raw`
import json
import sqlite3
import sys

def change_type(raw_event):
    document = json.loads(raw_event)
    change = document.get("change")
    return change.get("type") if isinstance(change, dict) else document.get("type")

def count_slot_rows(connection, table, slots):
    return sum(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE creator_account_id=? AND projection_slot=?",
            (row[0], row[1]),
        ).fetchone()[0]
        for row in slots
    )

def slot_document(row):
    return {
        "creatorAccountId": row[0],
        "projectionSlot": row[1],
        "generationId": row[2],
        "projectedCanonicalRevision": row[3],
        "readRevision": row[4],
    }

canonical = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
projection = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro", uri=True)
try:
    event_rows = canonical.execute(
        "SELECT event_id, source_seq, event_json FROM raw_ingest_events ORDER BY source_seq"
    ).fetchall()
    event = canonical.execute(
        "SELECT COUNT(*), COUNT(DISTINCT source_seq), MIN(source_seq), MAX(source_seq) "
        "FROM raw_ingest_events"
    ).fetchone()
    checkpoint = canonical.execute(
        "SELECT COUNT(*), COALESCE(MAX(committed_source_seq), 0) FROM ingest_checkpoints"
    ).fetchone()
    view_revision = canonical.execute(
        "SELECT COALESCE(MAX(view_revision), 0) FROM account_heads"
    ).fetchone()[0]
    activations = canonical.execute(
        """SELECT h.creator_account_id,i.generation_id,i.activated_view_revision
             FROM account_heads h
             JOIN projection_activation_intents i
               ON i.creator_account_id=h.creator_account_id
              AND i.state='activated'
              AND i.activated_view_revision=h.view_revision
            WHERE i.target_canonical_revision=(
                      SELECT MAX(i2.target_canonical_revision)
                        FROM projection_activation_intents i2
                       WHERE i2.creator_account_id=i.creator_account_id
                         AND i2.state='activated'
                         AND i2.activated_view_revision=h.view_revision
                  )
            ORDER BY h.creator_account_id"""
    ).fetchall()
    projection_accounts = projection.execute(
        """SELECT creator_account_id,projection_slot,generation_id,
                  projected_revision,read_revision
             FROM projection_accounts
            ORDER BY creator_account_id,projection_slot"""
    ).fetchall()
    active_slots = []
    for account_id, generation_id, activated_view_revision in activations:
        matches = [
            row for row in projection_accounts
            if row[0] == account_id and row[2] == generation_id
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"canonical activation {account_id}/{generation_id} resolved to "
                f"{len(matches)} projection slots"
            )
        if matches[0][4] != activated_view_revision:
            raise RuntimeError(
                f"canonical activation {account_id}/{generation_id} read revision mismatch"
            )
        active_slots.append(matches[0])
    active_keys = {(row[0], row[1]) for row in active_slots}
    inactive_slots = [
        row for row in projection_accounts if (row[0], row[1]) not in active_keys
    ]
    slot_counts = projection.execute(
        """SELECT creator_account_id,COUNT(*)
             FROM projection_accounts
            GROUP BY creator_account_id
            ORDER BY creator_account_id"""
    ).fetchall()
    projected_read_revision = max((row[4] for row in active_slots), default=0)
    projected_canonical_revision = max((row[3] for row in active_slots), default=0)
    result = {
        "streamCount": checkpoint[0],
        "committedSourceSeq": checkpoint[1],
        "eventCount": event[0],
        "distinctEventSequenceCount": event[1],
        "minimumEventSequence": event[2],
        "maximumEventSequence": event[3],
        "canonicalChatCount": canonical.execute(
            "SELECT COUNT(*) FROM account_chats WHERE is_deleted=0"
        ).fetchone()[0],
        "canonicalMessageCount": canonical.execute(
            "SELECT COUNT(*) FROM account_messages WHERE is_deleted=0"
        ).fetchone()[0],
        "readModelChatCount": count_slot_rows(
            projection, "conversation_summaries", active_slots
        ),
        "readModelMessageCount": count_slot_rows(
            projection, "projection_messages", active_slots
        ),
        "messageAnalysisCount": count_slot_rows(
            projection, "projection_message_analysis", active_slots
        ),
        "lpgNodeCount": count_slot_rows(
            projection, "projection_lpg_nodes", active_slots
        ),
        "lpgEdgeCount": count_slot_rows(
            projection, "projection_lpg_edges", active_slots
        ),
        "inactiveReadModelChatCount": count_slot_rows(
            projection, "conversation_summaries", inactive_slots
        ),
        "inactiveReadModelMessageCount": count_slot_rows(
            projection, "projection_messages", inactive_slots
        ),
        "inactiveMessageAnalysisCount": count_slot_rows(
            projection, "projection_message_analysis", inactive_slots
        ),
        "inactiveLpgNodeCount": count_slot_rows(
            projection, "projection_lpg_nodes", inactive_slots
        ),
        "inactiveLpgEdgeCount": count_slot_rows(
            projection, "projection_lpg_edges", inactive_slots
        ),
        "projectionSlotCount": len(projection_accounts),
        "maximumProjectionSlotsPerAccount": max(
            (row[1] for row in slot_counts), default=0
        ),
        "projectionSlotCountsByAccount": [
            {"creatorAccountId": row[0], "slotCount": row[1]}
            for row in slot_counts
        ],
        "activeProjectionSlots": [slot_document(row) for row in active_slots],
        "inactiveProjectionSlots": [slot_document(row) for row in inactive_slots],
        "viewRevision": view_revision,
        "projectionReadRevision": projected_read_revision,
        "projectedCanonicalRevision": projected_canonical_revision,
        "canonicalAccountIds": [row[0] for row in canonical.execute(
            "SELECT DISTINCT creator_account_id FROM account_chats ORDER BY creator_account_id"
        )],
        "projectionAccountIds": [row[0] for row in projection.execute(
            "SELECT DISTINCT creator_account_id FROM projection_accounts ORDER BY creator_account_id"
        )],
        "eventIds": [row[0] for row in event_rows],
        "eventSequences": [row[1] for row in event_rows],
        "eventChangeTypes": [change_type(row[2]) for row in event_rows],
    }
    result["eventSequenceIsContiguous"] = (
        event[0] == 0
        or (
            event[0] == event[1]
            and event[3] - event[2] + 1 == event[0]
        )
    )
    print(json.dumps(result, separators=(",", ":")))
finally:
    projection.close()
    canonical.close()
`;

export async function readSqliteProof({ canonicalDatabasePath, projectionDatabasePath }) {
  const { stdout } = await execFileAsync(
    pythonExecutable(),
    ['-c', PROOF_SCRIPT, canonicalDatabasePath, projectionDatabasePath],
    { windowsHide: true, maxBuffer: 64 * 1024 },
  );
  return JSON.parse(stdout);
}
