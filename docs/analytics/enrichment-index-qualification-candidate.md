# Enrichment identity index: requested qualification candidate

The original dfd1da8 feasibility experiment saved 246.666 ms and was not adopted
under its 300 ms minimum rule. That result and decision are retained. The user
subsequently requested qualification of this candidate despite that decision.
This branch implements the exact experimental index as normal migration 0025,
not benchmark-only DDL. It is an isolated, unpushed candidate, not a deployment.

The existing composite primary key, payloads, triggers, foreign keys, cleanup,
proof lifetimes, cache scopes and scheduler remain unchanged. Catalog 25 is
registered with the exact catalog-24 trigger signatures. The changed SQLite schema
version still invalidates older proof receipts. The package SQL catalog binds the
new migration's hash; that is not packaged-qualification evidence.

Required checks include populated encrypted upgrade/backup, failed-migration
rollback, old-catalog rejection, unchanged artifacts and stored bytes, receipt
invalidation, missing-index fallback, missing-trigger rejection, reference/FK
checks and shared/unshared reclamation. No migration is injected by the harness.

Use external signed harness 334a019, keeping source latency/correctness and
isolation coverage separate. Run original v7 repetition 1 first, then 0 and 2 only
on success. All twelve probes must satisfy the original ten-second criterion.
There is no new numerical threshold, exemption, cached PASS or automatic retry.
The previous diagnostic saving must not be subtracted from a historical failure.

Primary references: https://www.sqlite.org/lang_createindex.html and
https://www.sqlite.org/foreignkeys.html. These establish index semantics, not a
performance result for this candidate.
