#!/usr/bin/env python3
"""JSON-mode entrypoint for RET-001 cross-boundary observations.

The Extension emits protocol JSON. Production transport validates those payloads
in JSON mode, so this entrypoint preserves that behavior while reusing the
scenario implementation in execute-ret-001-cross-boundary-observations.py.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.protocol.payloads import (
    IngestDeltaPayload,
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
)
from ret001_execution import canonical_json, load_module

MODULE_PATH = Path(__file__).with_name("execute-ret-001-cross-boundary-observations.py")
module = load_module("ret001_cross_boundary_observations", MODULE_PATH, register=False)


def ingest_snapshot_json(history, account_id: str, stream_id: str, snapshot: dict) -> dict:
    key = module.stream_key(account_id, stream_id)
    bound = module.identity(account_id, stream_id)
    begin = IngestSnapshotBeginPayload.model_validate_json(
        canonical_json({**bound, **snapshot["begin"]})
    )
    begin_result = history.begin_snapshot(key, begin)
    chunk_results = []
    for document in snapshot["chunks"]:
        chunk = IngestSnapshotChunkPayload.model_validate_json(
            canonical_json({**bound, **document})
        )
        chunk_results.append(history.add_snapshot_chunk(key, chunk))
    commit = IngestSnapshotCommitPayload.model_validate_json(
        canonical_json({**bound, **snapshot["commit"]})
    )
    commit_result = history.commit_snapshot(key, commit)
    return {
        "begin": begin_result.status,
        "chunks": [result.status for result in chunk_results],
        "commit": commit_result.status,
        "committed_source_seq": commit_result.committed_source_seq,
        "snapshot_committed": commit_result.snapshot_committed,
    }


def ingest_delta_json(history, account_id: str, stream_id: str, item: dict):
    document = {
        **module.identity(account_id, stream_id),
        "event_id": UUID(item["event_id"]),
        "source_seq": item["source_seq"],
        "acquisition_origin": item.get("acquisition_origin", "passive"),
        "change": item["change"],
    }
    payload = IngestDeltaPayload.model_validate_json(canonical_json(document))
    return history.commit_delta(module.stream_key(account_id, stream_id), payload)


module.ingest_snapshot = ingest_snapshot_json
module.ingest_delta = ingest_delta_json

if __name__ == "__main__":
    raise SystemExit(module.main())
