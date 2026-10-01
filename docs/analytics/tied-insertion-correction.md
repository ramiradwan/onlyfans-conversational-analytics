<!-- CODE-VERIFY: Check conversation_insertion.py, conversation_enrichment_insertion.py, tied_insertion_metrics.py and their safety/diagnostic tests before editing the conditions or commands. -->
# Tied-insertion correction

This correction removes whole-history metric object reconstruction for an eligible terminal tied insertion. It does not change the analytics schema, proof lifetime, source checks, publication authority, cleanup or qualification gates.

## Construction

`match_inserted_source` still checks every prior source/enrichment identity, ordering position, direction, account and participant binding. Removing the new message must still reproduce the predecessor's complete input digest. It no longer eagerly allocates a metric input for every historical row.

`build_inserted_metrics` first checks the changed suffix through `tied_suffix_metrics`. The shortcut requires a nonempty suffix no larger than the existing page bound. Every suffix message must have the same final timestamp, direction and sentiment score as the inserted message, with the expected shifted ordinals and account/conversation/participant bindings.

These conditions make the insertion equivalent to an append for the metric calculation. The direction sequence, response samples, silence gaps and chronological bounds do not change. Equal suffix scores preserve the exact floating-point score sequence. The existing append calculation must also establish an unambiguous rounded result. No exact-sum fallback in a different order is used.

Any rejected or ambiguous condition retains the full metric calculation in the actual insertion order. New analysis still uses the ordinary analyzer and configuration checks.

## Persisted validation

The validator reads and digest-checks the actual predecessor and candidate frames. It requires the same header, generation, configuration, account and retention bindings as before. The prefix must be byte-identical. Every shifted suffix row must equal its predecessor except for the required ordinal increment. Added-reference uniqueness, confidence totals, analyzer entries and the resulting unit digest remain checked.

Ordinals must still be integers at their exact positions, and stored timestamps must still be strings for incremental admission. Unescaped, uniquely named canonical fields use lexical checks. Other encodings use JSON parsing. In particular, a numerically encoded timestamp continues to refuse incremental validation and use the complete validator. The correction does not turn a previously unsupported encoding into permission to reuse it.

Metrics are calculated again from the persisted selection and compared with the candidate header. The validator does not accept the constructor's calculation as its expected result. Only an existing independently checked predecessor permits prefix reuse; restart or missing-proof behavior is unchanged. There is no new retained decoded-frame cache or PASS cache. The existing failed append trial may still decode frames before insertion validation; this pass does not claim to remove every frame read.

## Diagnostic comparison

Use the same `light_first_update_benchmark.py` and `analytics_insertion_diagnostic.py` bytes on baseline and candidate. The diagnostic follows the production metric helper when available and the original production calculation on the baseline. Preserve all samples, alternating append/insertion order, immutable predecessor state and fresh independent source recomputation per sample.

Compare insertion with insertion to measure the correction. Append remains a separate unchanged-path control. Instrumented work counts explain the change; `--trace-mode none` supplies an unpatched timing comparison. The full-size component excludes graph/lifecycle/disk costs. A shortened scheduled check confirms actual dispatch and shutdown, not the original post-idle 100k visibility gate.

The exact v7 prefix and complete qualification campaign remain separate acceptance work. No component or shortened scheduled result qualifies A07.

## Measurement design references

Python's [time documentation](https://docs.python.org/3.13/library/time.html) distinguishes elapsed and current-thread CPU clocks. SQLite's [isolation documentation](https://www.sqlite.org/isolation.html) distinguishes stable cross-connection snapshots from writes visible on the same connection. This correction retains the caller's validation transaction and does not carry a read result beyond it.
