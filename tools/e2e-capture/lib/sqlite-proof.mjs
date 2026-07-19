import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import { pythonExecutable } from './paths.mjs';

const execFileAsync = promisify(execFile);

const PROOF_SCRIPT = String.raw`
import json
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
try:
    event_rows = database.execute(
        "SELECT event_id, source_seq, event_json FROM raw_ingest_events ORDER BY source_seq"
    ).fetchall()
    event = database.execute(
        "SELECT COUNT(*), COUNT(DISTINCT source_seq), MIN(source_seq), MAX(source_seq) "
        "FROM raw_ingest_events"
    ).fetchone()
    checkpoint = database.execute(
        "SELECT COUNT(*), COALESCE(MAX(committed_source_seq), 0) FROM ingest_checkpoints"
    ).fetchone()
    view_revision = database.execute(
        "SELECT COALESCE(MAX(view_revision), 0) FROM account_read_models"
    ).fetchone()[0]
    result = {
        "streamCount": checkpoint[0],
        "committedSourceSeq": checkpoint[1],
        "eventCount": event[0],
        "distinctEventSequenceCount": event[1],
        "minimumEventSequence": event[2],
        "maximumEventSequence": event[3],
        "canonicalChatCount": database.execute("SELECT COUNT(*) FROM canonical_chats").fetchone()[0],
        "canonicalMessageCount": database.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0],
        "readModelChatCount": database.execute("SELECT COUNT(*) FROM read_model_chats").fetchone()[0],
        "readModelMessageCount": database.execute("SELECT COUNT(*) FROM read_model_messages").fetchone()[0],
        "viewRevision": view_revision,
        "eventIds": [row[0] for row in event_rows],
        "eventSequences": [row[1] for row in event_rows],
        "eventChangeTypes": [json.loads(row[2]).get("type") for row in event_rows],
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
    database.close()
`;

export async function readSqliteProof(databasePath) {
  const { stdout } = await execFileAsync(
    pythonExecutable(),
    ['-c', PROOF_SCRIPT, databasePath],
    { windowsHide: true, maxBuffer: 64 * 1024 },
  );
  return JSON.parse(stdout);
}
