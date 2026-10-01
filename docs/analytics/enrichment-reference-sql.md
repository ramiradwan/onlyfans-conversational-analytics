<!-- CODE-VERIFY: analytics_enrichment_reference_sql.py, light_first_update_benchmark.py and test_enrichment_reference_component.py own this diagnostic. -->
# Enrichment reference SQL diagnostic

Use the existing light runner with `--preparation focused-component --component-kind reference-sql --reference-sql-kind enrichment`. The default case is the dominant tied insertion; `--enrichment-reference-case dominant-append` and `small-append` provide controls. The fixture uses the 101-conversation source shape and actual bounded analyzer-cache records, not empty or padded blobs.

The complete transition includes source-independent prepared-unit storage, 100 unchanged reference copies, four complete ordered reference reads/comparisons, synchronous retirement and reclamation, cache-scope restoration, three commits and their connection closes. The component declares its independently checked header proofs explicitly; it does not establish canonical publication authority or full graph/scheduler visibility. Full independent source recomputation and stored-content validation execute after every sample and are timed separately.

A closed encrypted seed is hash-checked and copied into a separate sample directory before each sample. The reset is not timed as production work. Each sample commits normally. No indexed subject alone receives a warmup. Query plans use a separate disposable copy. The output records serialized payload sizes, analyzer-record counts, schema/runtime settings, query programs, copied/selected row counts, migration/index creation time and all timings. Process memory is bounded by the existing runner.

`--enrichment-index-probe` is permitted only for this component on catalog 24. It adds the proposed identity index to the disposable populated fixture without changing production source or pretending a migration occurred. It is only a feasibility experiment. No query hints, altered planner statistics, disabled foreign keys or cached expected results are introduced.

First compare the normal fixture and index probe on the same native Windows runtime. If the access path does not switch or the complete savings cannot reach 300 ms, stop without a runtime migration. Otherwise use the same helpers for the production candidate comparison. A component success never changes the original failed visibility or isolation receipts.

Primary design references: https://www.sqlite.org/withoutrowid.html, https://www.sqlite.org/foreignkeys.html and https://www.sqlite.org/eqp.html. These explain possible index benefits; only the recorded experiment establishes the local result.
