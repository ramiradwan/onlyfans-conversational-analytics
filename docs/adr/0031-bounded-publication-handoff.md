# ADR 0031: Use stored references for analytics publication

- Status: accepted

## Decision

A staged SQLite generation crosses the worker boundary as an immutable reference. The reference binds the account, generation, canonical identity, pipeline identity, projection and graph digests, publication epoch, and source expiry.

The receiving worker checks that reference against persisted generation metadata. Staging and the final activation gate independently verify stored content. A failure at the final gate reconciles the canonical witness and retires the candidate before it becomes visible. Publication independently verifies the candidate's stored content and retains its canonical witness and ownership checks. A reference is not proof that its rows are correct.

SQLite publication does not reconstruct a complete graph merely to return a result to the scheduler. Explicit artifact reads resolve the exact referenced generation, revalidate its content and source, and refuse expired, discarded, retired, or changed data. The direct `project_account` operation materializes its staged snapshot before publication and returns that snapshot. Memory stores and catalogs without query metadata retain inline artifact handoffs.

Persisted graph verification validates and hashes one row at a time unless the caller explicitly requests graph objects. Endpoint closure is checked by indexed joins in the same snapshot. The canonical graph digest format is unchanged.

Validation-only gates return a distinct projection metadata header after checking each stored array record and reconstructing the same canonical digest. They do not retain complete message or conversation model arrays. Full projection reads still materialize them. The canonical stored field order uses streaming verification; other supported orders and omitted defaults retain the public model compatibility path. No supplied digest substitutes for reading and validating the records. Publication retention checks and timer setup derive their earliest source time from the same streamed records; retirement cleanup processes one stored document at a time.

Projection JSON and its digest encode message and conversation arrays one record at a time. They preserve the existing canonical JSON bytes. The stored projection document remains complete; this does not introduce a different public schema.

The built-in pipeline validates privately owned graph objects record by record and passes them to the owned writer without a complete clone. Public writer inputs remain subject to validation. Fragment validation during SQLite staging consumes one fragment at a time inside the transaction. A failed fragment rolls back the stage.

## Consequences

This removes duplicate handoff and verification representations, not every account-sized allocation. Construction, the stored projection document, graph replacement, and explicit full artifact reads remain proportional to the account. Physical reuse of unchanged graph data requires a separate storage design.

No new dependency, database, model, or download is required. The existing migration catalog must be included in the package. Authorization, durable commits, foreign keys, property checks, source expiry, publication fencing, and backup checks remain required.

[Bounded publication](../analytics/bounded-publication.md) defines caller behavior and qualification. ADRs 0020, 0028, 0029, and 0030 retain their publication and integrity requirements.
