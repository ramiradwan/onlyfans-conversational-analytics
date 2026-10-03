# ADR 0030: Separate generation validation from whole-file integrity checks

- Status: accepted

## Decision

Validate each candidate's projection, graph properties, identities, digests, counts, and endpoint closure before publication. Recheck the persisted candidate before completing local activation. Each check reads one consistent database snapshot.

Run SQLite's whole-file integrity and foreign-key checks when reconciling the store at startup or recovery. Backup validation retains its independent whole-file checks. Ordinary reads and publication do not rescan unrelated active or retired generations solely to verify one candidate.

Candidate validation explicitly checks graph endpoints and rejects child records assigned to a different account. SQL foreign keys, immutable-record triggers, source identity, completed witnesses, and publication ownership remain enforced. Physical corruption encountered by a read still fails the operation. Corruption confined to unrelated records is detected by whole-file verification, not by reading another account's candidate.

Graph records constructed and validated from stored rows can be encoded directly for their digest within that check. Public graph inputs are always revalidated. No validated object is shared across requests or retained as a trusted mutable cache.

A graph writer keeps one write connection for the duration of its lease session, separate from the heartbeat connection. Each bounded transaction renews and checks ownership before and after its writes. Successful commits need no additional wait for a heartbeat. Batch sizes can grow to a bounded maximum or shrink after slow work.

## Consequences

The generation and canonical digest formats are unchanged. Startup and explicit recovery can still take time proportional to the database size. Full-generation construction, validation, serialization, and replacement remain required.

This changes where file-wide verification runs, not which generation becomes visible. It does not grant analysis authority, extend retention, or change backup trust. It adds no dependency, database, or model.

[Generation throughput](../analytics/generation-throughput.md) describes the checks, bounds, and measurements. The publication and ownership rules in ADRs 0020, 0028, and 0029 remain in force.
