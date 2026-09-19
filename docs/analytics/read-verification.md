<!-- CODE-VERIFY: Check source_tokens.py, canonical_source.py, query_reply_source.py, query_publication.py, graph_privacy.py, and the source-token and query-metadata migrations before changing claims. -->

# Verify sources without repeating unchanged work

Canonical identity remains a digest of the complete canonical account document. The source-token cache reuses only that scalar identity, not messages, graph results, or authorization decisions.

## Source changes

The canonical database replaces an account's token when its messages, chats, canonical revision, tombstones, deletion barriers, or participant deletion scopes change. Updates cover the old and new account when a record moves. Rollback also rolls back token changes.

The gateway verifies the installed tracking-trigger definitions when the schema changes. Missing or altered definitions disable caching. Identity lookup requires the same account, source token, canonical revision, and schema. A content edit invalidates reuse even when it does not advance the canonical revision or update the message's stored hash.

Each gateway instance keeps at most eight entries for 60 seconds. Entries contain an opaque account key, token, revision, schema number, and content digest. No input text or native conversation/message identifiers are retained. Expired entries are removed on the next cache operation. Reads do not extend their lifetime.

A cache miss scans canonical content. A supplied database connection always reads its own transaction and never uses this cache. Question execution still checks for concurrent canonical changes and rechecks the publication witness before returning.

The cache does not authorize a build, make a stale generation readable, or bypass source expiry. Cold requests can still exceed their limits; background verification can populate the cache without executing a question.

## Bounded reply selection

The reply query selects one message within the requested period and all messages tied at the latest retained timestamp through the analysis cutoff. It does not load every message in each selected conversation.

A canonical date index performs the coarse selection. Exact timezone-aware comparisons preserve inclusive starts, exclusive ends, source expiry, cutoff boundaries, and microsecond ties. A later reply outside the selected date range remains relevant. Large tie sets still exhaust the record budget rather than being silently truncated.

This reduction applies to the canonical adapter's inferred ordering and unknown event kinds. It does not infer that an unknown event is a human message. Pricing keeps its separate selection and quality gate. Query record and execution-time limits are unchanged.

## Projection metadata and hashing

Question reads use a small generation-bound metadata row instead of parsing the full projection document for its count, sequence, and earliest source time. Insert triggers derive these values from the document. Independent inserts must match it; updates and active deletion are refused. Retirement and generation deletion preserve cleanup.

Graph hashing validates and encodes one record at a time in canonical order. It produces the same digest as encoding the complete JSON document. Cancellation is checked between records. Publication still validates the full graph and writes a complete generation.

## Verification

```powershell
python -m pytest tests/test_analytics_source_tokens.py tests/test_reply_source_selection.py tests/test_projection_query_metadata.py tests/test_graph_digest_stream.py
python tools/qualify_analytics_baseline.py --output C:\temp\verified-baseline
python tools/qualify_continuous_analytics.py --messages 10000 --query-samples 100 --output C:\temp\verified-workload
```

Output directories must be new. Run the canonical ingestion, backup, migration, and analytics regression tests because the change includes an authoritative schema migration. Report query failures separately from successful latency. These synthetic measurements do not qualify production event classification, pricing quality, constrained laptops, or installer size.
