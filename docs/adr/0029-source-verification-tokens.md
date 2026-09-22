# ADR 0029: Bind cached source verification to database changes

- Status: accepted

<!-- CODE-VERIFY: Check source_tokens.py, canonical_source.py, pipeline.py, projection_activation.py, and migration 0009 before changing source-proof claims. -->

## Decision

Add per-account invalidation tokens to the canonical database. Source message, chat, account-revision, tombstone, and deletion-scope changes replace their token in the same transaction. Tokens are random identifiers, not content hashes or permission grants. Publication bookkeeping does not change them.

The analytics gateway may reuse a previously computed canonical digest only when the account, token, revision, and verified tracking schema still match. The process-local cache holds at most eight opaque account keys and scalar identities for 60 seconds. It stores neither message text nor conversation identifiers. Reads do not extend the deadline. Missing or altered tracking triggers disable reuse.

A full scan may also mint a process-local HMAC proof that binds the account, exact canonical identity, and source token. A later build or witness gate can reuse that scanned identity after the ordinary 60-second cache entry expires only when it independently reads the same current token. Restart invalidates the HMAC key. Missing tracking, malformed proof, a different token, or a different identity uses the existing scan or changed-source path.

A cache miss performs the existing exact content scan under the caller's budget. Supplied transaction connections always scan their own data and never share cached identities. The canonical digest format, activation witness, deletion rules, and analysis admission are unchanged. Source tokens and process proofs cannot make an unverified generation visible.

Use an indexed date filter to select range evidence and all latest-message timestamp ties for reply questions. Verify exact instants after the coarse SQL filter. Keep inferred ordering, unknown message kinds, and history coverage explicit. Bound returned source records and SQL execution time; do not raise existing query limits.

Persist a small, immutable projection metadata row for question reads. Database triggers derive it from the projection document and reject inconsistent writes. Its lifetime follows the owning generation. Full graph and document validation remain required for publication.

## Consequences

The canonical database gains an additive migration, a source-token table, tracking triggers, and a date index. Existing conversation records are unchanged. The analytics database gains an additive metadata table and triggers. Normal migration backup and newer-schema refusal remain required.

This narrows repeated verification work without making database tokens authoritative facts. A cold or expired identity cache may still exhaust a bounded request. Background currentness checks can populate it independently of question requests.

Graph hashing encodes validated records one at a time, preserving the exact canonical JSON bytes and cancellation checks. Full-generation serialization, validation, and physical writes remain costs. No new model, dependency, process, or cloud service is introduced.

[Read verification](../analytics/read-verification.md) defines cache behavior and tests. ADRs 0009, 0019, 0020, 0026, and 0028 retain their authority, encryption, publication, and data-use rules.
