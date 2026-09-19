# ADR 0036: Bind activation to a verified content epoch

- Status: accepted

## Decision

Staging independently validates the candidate's persisted projection and graph. A process-local receipt can carry that result to activation when the database proves that the checked content has not changed.

The analytics database maintains a monotonic content epoch through insert, update and delete triggers on projection, graph, cache and query-metadata tables. The receipt binds the exact store, schema cookie, content epoch and generation metadata. It is captured only when the stamp is unchanged across validation and retained only after the validation transaction commits.

Activation consumes a receipt once, inside its existing immediate transaction. That transaction excludes competing writers until the active-generation change commits. The receipt expires after 60 seconds; at most eight receipts are retained. The exact tracking-trigger definitions and catalog version must match the reviewed contract. No receipt is persisted or accepted from a caller.

A missing, expired, evicted or mismatched receipt requires independent full validation. Changes to stored content, the schema, store identity or bound generation fields invalidate reuse. A future catalog version disables reuse until its tracked tables are reviewed. Startup, recovery, explicit artifact reads and backup retain their full validation paths.

## Authority and retention

The canonical witness, live source identity, publication epoch, current owner and predecessor compare-and-swap remain required. Receipts do not authorize analysis or bypass source-time expiry. Updates that only advance publication status, lease or witness metadata are checked by the existing activation protocol rather than treated as graph changes.

The content epoch covers logical SQL changes. It does not claim protection against a privileged process rewriting the encrypted file or deliberately replacing the schema and its version counters. SQLCipher integrity checks and the existing storage trust boundary remain in force.

## Consequences

This narrows ADR 0031's repeated-scan requirement: activation requires either an unchanged live receipt for an independently checked candidate or another complete check. It does not replace data validation with a comparison of supplied digest strings.

The new epoch adds write overhead, including on cold builds. Measurement must include that cost. Logical account construction, projection encoding and physical copying of cache data remain separate costs. The change adds one rebuildable analytics migration and no dependency, model, database file or writer process.

The canonical witness repository can separately reuse an identity it independently scanned. Its own eight-entry, 60-second cache checks the current canonical source token and tracking schema inside each witness transaction. It never accepts the analytics worker's supplied digest as a cache entry. Source edits, rollback, missing tracking, expiry and restart require a new scan.

## Scheduler currentness

Periodic reconciliation can reuse a positive result from a complete validated projection read. This separate process-local cache holds at most eight metadata proofs for 60 seconds without extending their lifetime on access. Each read checks the active generation, canonical identity, pipeline configuration, completed witness, content epoch and schema. Source expiry is checked against the original earliest message time on every hit.

An invalid, missing or changed proof uses the complete read path. A changed source revision or unavailable projection returns not-current and leaves recovery to normal admission. The cache does not return message or graph data, authorize analysis, or replace explicit projection reads. Missing tracking, restart and catalog changes disable reuse until an independent read succeeds.
