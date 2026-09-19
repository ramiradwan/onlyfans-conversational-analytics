# ADR 0028: Recompute changed conversations within full analytics generations

- Status: accepted

## Decision

Use conversation input digests to reuse unchanged metrics, message analysis, and conversation-local graph records. Store optional fragments in the existing encrypted analytics database. They are derived records, not canonical authority.

The canonical gateway streams the same account identity used by publication validation. It also records conversation digests, then loads changed conversations individually. Streaming does not remove the cost of reading canonical content to verify its digest.

A fragment is reusable only from a completed, active generation and for the exact account, conversation inputs, analysis configuration, metric definition, and retained source window. Undeclared analyzer inputs and custom graph projectors use the full computation path.

Combine the retained conversation parts into one graph. Resolve shared nodes by stable identity and reconstruct participant timelines from current conversation metrics. Removed conversations contribute no nodes or edges.

Publication remains a full, validated generation replacement. No partial graph becomes visible, and no correctness rule depends on an atomic transaction across databases. The canonical witness uses the unchanged identity format and admission rules.

The existing scheduler reconciles canonical state periodically as well as after notifications. It recovers missed notifications, failed startup attempts, and source expiry through normal analysis admission. Reconciliation stops with the scheduler. It is not a new broker, process, or authority.

## Consequences

Optional fragment records are bounded, immutable after staging, and removed when their generation retires or is deleted. Source expiry vetoes reuse. A cold or invalidated cache recomputes the permitted result.

Whole-generation serialization, validation, writes, and canonical digest scans remain. Reduced conversation computation does not establish a large-account latency or memory guarantee.

No runtime package, model download, public schema, or canonical migration is added. [Continuous analytics](../analytics/continuous-processing.md) defines limits, lifecycle behavior, and measurements.

The authority, encryption, publication, retention, and authorization requirements in ADRs 0009, 0019, 0020, 0026, and 0027 remain in force.
