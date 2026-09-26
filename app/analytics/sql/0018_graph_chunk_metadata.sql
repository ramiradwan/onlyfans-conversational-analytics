-- Cover chunk availability checks without reading canonical payloads.

CREATE INDEX graph_segment_chunk_headers ON graph_segment_chunks (
    creator_account_id,
    segment_id,
    kind,
    record_count,
    canonical_digest
);
