<!-- CODE-VERIFY: Check analytics_reference_sql_component.py, the existing light-runner option and its tests before changing the scope. -->
# Reference SQL component

Use the existing light runner with `--preparation focused-component --component-kind reference-sql --messages 100000 --focused-repeats 3 --trace-mode none`. Run it through shared host admission with a signed, clean source and a fresh output directory. Baseline and candidate use identical helper bytes and input identities.

The fixture has 101 conversations with deterministic compressed membership arrays and a dominant 50,001-message predecessor. It uses the full production migration catalog, foreign keys and triggers. Each sample stores one new unit, copies 100 unchanged references, checks graph-reference closure, and synchronously retires the predecessor. It verifies that the unshared old unit disappeared, shared units survived, foreign keys are valid, and cleanup flags are disarmed. Each sample regenerates its independent membership input and checks all actual stored headers, arrays and byte digests. The next sample rolls back to the same state; no previous PASS is cached.

This tests SQL/storage/reference semantics. It does not create a canonical account, a complete graph payload, a canonical activation witness or a scheduled publication. No graph-equivalence or visibility qualification claim follows from this fixture. Transaction durability, the 61-second idle, restart and full qualification are outside its scope.

The complete component interval includes changed-unit storage, reference copying, closure checking and retirement. Fresh independent verification and fixture rollback are separate. Query plans are collected outside the timed samples; no query-plan probe runs inside a scheduled interval. The recipe records actual planner output rather than requiring its display format in production.

SQLite documents covering indexes and warns that large-row WITHOUT ROWID tables have expensive B-tree search behavior. The disposable plan experiment found that a compact UNIQUE identity index is used by the foreign-key program as well as the reference-closure query. A wider nonunique metadata index and a NOT EXISTS rewrite alone did not remove that cost. Reference-first reclamation avoids visiting large units that are still shared.

References: https://www.sqlite.org/withoutrowid.html, https://www.sqlite.org/queryplanner.html, https://www.sqlite.org/foreignkeys.html and https://www.sqlite.org/eqp.html.


Recipe v2 uses the unchanged production cache scopes: 32 MiB during unit storage and validation, and 128 MiB during synchronous retirement. Cache entry/restoration is inside the complete transition. V1 used the connection default and is retained as exploratory evidence only, not the matched production-cache comparison. This is still a single-connection component; the exact lifecycle needs its own integration run.
