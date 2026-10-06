<!-- CODE-VERIFY: Check pipeline.py, test_readiness_identity.py, source_tokens.py, canonical_source.py, query_reply_source.py, query_publication.py, graph_privacy.py, and the source-token and query-metadata migrations before changing claims. -->

# Verify sources without repeating unchanged work

Canonical identity remains a digest of the complete canonical account document. The source-token cache reuses only that scalar identity, not messages, graph results, or authorization decisions.

## Source changes

The canonical database replaces an account's token when its messages, chats, canonical revision, tombstones, deletion barriers, or participant deletion scopes change. Updates cover the old and new account when a record moves. Rollback also rolls back token changes.

The gateway verifies the installed tracking-trigger definitions when the schema changes. Missing or altered definitions disable caching. Identity lookup requires the same account, source token, canonical revision, and schema. A content edit invalidates reuse even when it does not advance the canonical revision or update the message's stored hash.

Each gateway instance keeps at most eight entries for 60 seconds. Entries contain an opaque account key, token, revision, schema number, and content digest. No input text or native conversation/message identifiers are retained. Expired entries are removed on the next cache operation. Reads do not extend their lifetime.

Build-time cache misses scan canonical content. Question requests require a prepared identity: a missing or expired entry returns the existing preparing state instead of scanning inside the request budget. Missing source tracking reports an error. A supplied database connection always reads its own transaction and never uses this cache. Questions still check concurrent canonical changes and recheck the publication witness before returning.

A full source scan can mint a process-local HMAC proof bound to that exact identity and source token. Long build and publication paths re-read the current token before reusing the scanned digest. A matching proof refreshes the normal identity-cache entry without rescanning content; restart, missing tracking, invalid proof, or a changed token uses the existing scan or changed-source path. The optional post-publication refresh does not undo publication if it fails.

The cache does not authorize a build, make a stale generation readable, or bypass source expiry. Startup, periodic upkeep, and requested recovery prepare identities through the existing bounded scheduler executor. A separate owned timer keeps preparation running while projection verification is slow; it adds no thread pool and cannot declare a publication ready. An unchanged current publication needs identity preparation, not another rebuild.

Readiness checks compare canonical identity again after stored-generation verification. A changed identity prevents a ready result. Scheduler preparation independently rescans entries with at most 30 seconds remaining, leaving an unexpired identity usable while the scan runs. The new scan gets the unchanged 60-second lifetime only after cancellation checks and a fresh source-token check outside its read snapshot. Concurrent preparation is serialized, and waiting and scanning stop when the scheduler closes. Ordinary reads do not renew entries.

## Bounded reply selection

The reply query selects one message within the requested period and all messages tied at the latest retained timestamp through the analysis cutoff. It does not load every message in each selected conversation.

A canonical date index performs the coarse selection. Exact timezone-aware comparisons preserve inclusive starts, exclusive ends, source expiry, cutoff boundaries, and microsecond ties. A later reply outside the selected date range remains relevant. Large tie sets still exhaust the record budget rather than being silently truncated.

This reduction applies to the canonical adapter's inferred ordering and unknown event kinds. It does not infer that an unknown event is a human message. Pricing keeps its separate selection and quality gate. Query record and execution-time limits are unchanged.

## Projection metadata and hashing

Question reads use a small generation-bound metadata row instead of parsing the full projection document for its count, sequence, and earliest source time. Insert triggers derive these values from the document. Independent inserts must match it; updates and active deletion are refused. Retirement and generation deletion preserve cleanup.

Metadata insertion and its independent binding check use the generated [validation header](document-validation.md). The header is derived by validating every JSON item without retaining the complete array parse tree. The stored document and its independent publication verification remain complete.

Graph hashing validates and encodes one record at a time in canonical order. It produces the same digest as encoding the complete JSON document. Cancellation is checked between records. Publication still validates the full graph and writes a complete generation.

## Verification

```powershell
python -m pytest tests/test_analytics_source_tokens.py tests/test_reply_source_selection.py tests/test_projection_query_metadata.py tests/test_graph_digest_stream.py
python tools/qualify_analytics_baseline.py --output $env:ANALYTICS_EVIDENCE_DIR/verified-baseline
python tools/qualify_continuous_analytics.py --messages 10000 --query-samples 100 --output $env:ANALYTICS_EVIDENCE_DIR/verified-workload
```

Output directories must be new. Run the canonical ingestion, backup, migration, and analytics regression tests because the change includes an authoritative schema migration. Report query failures separately from successful latency. These synthetic measurements do not qualify production event classification, pricing quality, constrained laptops, or installer size.


## Startup validation handoff

For unchanged active modern-v2 data, the store's initial full streaming verification
may retain a bounded `StartupVerification` result. It is not a trust envelope and
is never serialized. Question readiness consumes it once only after rechecking the
full generation row, completed publication witness, physical database identity,
all four content-stamp components, canonical identity, pipeline/configuration,
retention and cancellation. Final checks outside the original read snapshot remain.
Proofs and the final envelope are constructed before installation and installed
under one shared re-entrant proof lock. Missing, changed, expired or unsupported
handoffs use complete verification; no caller may force reuse. The original
receipt lifetime and combined eight-generation retention are unchanged. Garbage
collection can invalidate a handoff by changing the epoch; it cannot renew trust.

The optional startup trace covers child validation, repository opening, SQLite
checks, full recomputation, recovery/garbage collection and question preparation.
It records bounded spans, parent relationships, counters and uncovered time.
`cold_readiness_seconds` stays inclusive. The trace freezes its interval at readiness and is finalized only after in-flight callbacks join. Spans crossing the boundary retain their actual observed end and report only their contribution before readiness. An incomplete trace is reported as such;
telemetry failure does not grant readiness or change the product verdict.
