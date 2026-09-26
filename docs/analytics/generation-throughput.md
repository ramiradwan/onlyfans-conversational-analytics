<!-- CODE-VERIFY: Check database.py, graph_privacy.py, sqlite_graph_store.py, sqlite_projection_store.py, tests/test_generation_throughput.py, and backup.py before changing validation or performance claims. -->

# Validate and publish analytics generations

Publication checks the candidate's complete persisted graph and projection. It does not run a whole-database scan of unrelated generations on every read or activation.

## Validation boundaries

Startup and recovery run SQLite integrity and foreign-key checks over the whole analytics file. Backup verification runs its own checks. Candidate verification reads a consistent transaction and validates the document, closed graph properties, identities, counts, digests, account ownership, and endpoint closure.

Graph rows are validated when they are constructed. Their privately owned objects can then be encoded without constructing and validating a second copy. Public graph inputs remain subject to full revalidation, including objects created through unchecked model copying.

A read returns the same projection and graph objects it just validated. It does not decode the document or fetch the entire graph again. No mutable validation cache is shared between reads.

Generation numbering uses the immutable metadata row bound to each projection document. Catalogs predating that metadata use their original document path. Missing metadata in a current catalog is an error, not a reset of the sequence.

## Write lifecycle

A lease session owns one data-write connection and one heartbeat connection. Every write transaction checks ownership and renews its lease. Neither connection survives the session. Concurrent use from another thread is refused.

Batches start at 500 records. Fast completed writes can grow batches up to 4,000 records; slow operations shrink them, with a floor of 64. Successful writes do not wait for a separate heartbeat before proceeding. Long computation remains covered by the heartbeat, and the next transaction checks ownership again.

Write sessions and retired-generation cleanup use a 16 MiB SQLite page-cache target by default. Shared-content insertion temporarily selects a record-count-based target up to 128 MiB, then restores the prior setting. Cleanup removes edges before nodes and the generation, inside one transaction. Interrupted cleanup rolls back.

Stored-generation verification temporarily uses a 32 MiB page-cache target on its connection. It restores the previous setting after success, failure or cancellation. It does not cache validation decisions or change transaction boundaries. These connection-local targets do not cap total process memory.

Synchronous durable commits, foreign keys, publication fencing, and atomic generation activation remain enabled. There is no partial public generation or second writer process.

## Verification

Run `python -m pytest tests/test_generation_verification_cache.py tests/test_generation_throughput.py tests/test_sqlite_graph_store.py tests/test_sqlite_projection_store.py` and the [analytics regression baseline](qualification.md). Include the short-lease, ownership-loss, cancellation, tamper, and backup tests.

Use [the continuous workload command](continuous-processing.md#verification-and-measurement) for comparable timings. Record candidate build, publication, cleanup, memory, and query failures separately. Profiling adds overhead; compare unprofiled runs for latency. Passing correctness checks does not establish the 100,000-message or constrained-laptop targets.
