<!-- CODE-VERIFY: Check shared_graph.py, incremental_graph.py, graph_verification.py, pipeline.py, projection_store.py, sqlite_projection_store.py and test_incremental_graph_units.py before changing graph-root, fallback or compatibility claims. -->

# ADR 0041: Compose graph identity from verified segment digests

- Status: accepted

## Decision

Pipeline revisions containing `graph.segment-root.v1` use a versioned graph identity composed from the ordered graph-segment manifest instead of hashing the complete canonical graph byte stream.

The root binds each selected segment's record kind, bucket, record count and segment digest under a dedicated domain separator. Segment digests still bind the ordered graph record identities and their canonical content hashes. The root therefore changes when segment membership, record content, record count, kind or bucket changes.

Legacy pipeline revisions keep the existing canonical-byte graph SHA-256 definition. The public `graph_digest` field remains a SHA-256 digest string; its interpretation is selected by the pipeline revision.

## Incremental reuse

An eligible incremental build may reuse an unchanged predecessor segment's verified digest, count, category counts and chunk digest without opening that segment's canonical chunk solely to construct the final graph root.

Changed buckets still open any required predecessor chunk, apply additions/removals, recompute canonical record hashes, recompute the segment digest and write a replacement immutable segment.

The final root is computed from the complete candidate segment manifest. A one-conversation update therefore reads graph bytes only for affected buckets plus bounded manifest metadata.

## Verification and fallback

A reused segment is accepted only under the existing process-local graph-segment proof for the exact completed active predecessor. Segment identity, digest, count and immutable chunk proof must still match.

Cold builds, process restart, missing proof, unsupported storage and explicit full verification do not trust stored root metadata. They scan persisted graph rows, reconstruct deterministic segment digests from the checked canonical rows and then compute the same versioned root.

Endpoint closure, graph account scope, generation ownership, canonical witness checks, projection publication checks and activation receipts are unchanged. Explicit graph reads still materialize and validate complete graph rows.

Clean rebuilds must produce the same segment root as incremental construction.

## Consequences

Incremental graph identity construction is proportional to changed segment bytes plus the bounded segment manifest instead of total graph bytes.

The first build after adopting the new pipeline revision is a normal rebuild because the pipeline identity changes. Existing stored generations retain their legacy digest semantics.

The change adds no migration, external dependency, database file, service or writer process.
