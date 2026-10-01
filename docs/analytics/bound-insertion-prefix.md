<!-- CODE-VERIFY: Check enrichment_prefix.py, conversation_sql.py, conversation_insertion.py, conversation_enrichment_unit_sql.py, conversation_enrichment_insertion.py and their tests before editing the guarantees below. -->
# Bound enrichment-prefix reuse

This correction reuses an unchanged enrichment prefix during an eligible tied insertion and avoids reading the same stored message frames twice during append/insertion validation. It changes neither the persisted format nor the qualification protocol.

## Construction binding

The existing fragment reader must have an active generation, its completed canonical witness, and independently verified graph/enrichment headers. The new reader methods return one actual stored enrichment unit and accept only that same object while the reader is open. A copied header or caller-created unit is not authority. A failed subsequent read clears the earlier selection; closing the reader clears its retained unit.

For conversations with at least 1,024 predecessor messages, the bound matcher checks the insertion's existing page-sized suffix. The complete reconstructed old source must still match the independently verified predecessor input digest. Account, conversation, participant, unread count, message count, duplicate identity, ordering, timestamp and suffix-limit checks remain. The caller still checks pipeline configuration, source retention, graph/enrichment proofs, current source identity and publication authority.

This reuses the independently source-bound prefix rather than decoding all of its enrichment objects again. It does not trust an arbitrary digest or skip the full source digest calculation. Missing binding or failed eligibility returns to the ordinary construction path. Smaller conversations retain the existing full matcher.

## Stored validation

`validate_one_added_unit` first checks the existing append/insertion header conditions. It loads the actual predecessor for the specified generation and account, checks its complete header, and decodes and digest-checks both actual message frames. Those two frames are local to this call. Both dispatch paths use them; no constructor-provided decoded object or expected answer is accepted.

The unchanged prefix is still compared with the candidate's persisted bytes. Each shifted suffix row is verified, and the inserted message, metrics, confidence totals, analyzer entries, unit identity and retention bounds are checked independently. Public append/insertion validators retain their separate stored-read behavior. Materialized verification and cases without a trusted predecessor still use the complete validator.

Ordinal and timestamp-encoding admission runs in blocks of at most 256 records and 1 MiB of temporary joined bytes. Escapes, alternate formatting and duplicate/shadowed fields use the existing per-record parser rule. A larger individual row uses that fallback directly. Integer ordinals and string timestamps are not broadened by the fast path. Cancellation is checked between bounded blocks and again after frame reads.

## Validation and measurement

Use the existing insertion component on the unchanged-runtime baseline and candidate with identical diagnostic helper bytes, runtime, fixture, input/output digests and fixed sample order. Every sample still runs a fresh independent source recomputation and complete persisted-content check. The component records construction, storage and validation separately, plus their complete transition. Component success cannot qualify scheduler visibility.

Before a latency claim, repeat the exact unprofiled repeat-1 prefix. Preserve its original verification placement, forced rebuild, small update, 61-second idle, stale-reference check, synchronous cleanup, backlog drain and joined shutdown. Any verified miss remains a failure; no sample is retried or averaged away. A complete visibility campaign remains a separate acceptance step.

Safety tests cover changed source fields, duplicate identities, suffix boundaries, missing/copied/closed-reader bindings, failed subsequent reads, altered predecessor data, integer/timestamp encodings, cancellation, and single-read stored dispatch. Existing corruption, retention, generation-proof, incremental-enrichment and append/insertion tests remain in force.

Python's JSON decoder retains the last repeated object key; alternate and escaped encodings must therefore use the existing parser rule rather than an ambiguous field search. SQLite isolates separate transactions, not successive operations on one connection. This correction keeps decoded validation frames within one call and retains the caller's existing storage/proof checks.

Primary references: https://docs.python.org/3.13/library/json.html and https://www.sqlite.org/isolation.html.
