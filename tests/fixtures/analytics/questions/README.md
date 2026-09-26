# Analytics question examples

These synthetic cases contain manually specified expected answers for the [question contract](../../../../docs/analytics/questions.md). They are not captured conversations, wire fixtures, a training set, or evidence of language-model quality.

`messages` describes source records and selected classifier outputs. `account` is the authenticated context. Source IDs are synthetic aliases; an adapter may encode them as opaque references but must not derive expected answers from the production query or rebuild implementation.

`order` describes source order only when `ordering` is `source`. Inferred ordering cannot resolve an outcome-changing timestamp tie. `replays` instructs an ingestion-based harness to redeliver the named synthetic message without creating another canonical record.

`version` identifies the source message version. `finding_version` identifies the version used by the classification. A mismatch invalidates that classification. Deleted messages remain in these inputs to exercise filtering; they cannot provide result evidence.

`coverage` describes available source history, independently of query availability and classifier coverage. `now` is the retention clock, not the analysis cutoff. Matching IDs and undetermined IDs are distinct conversation sets. `evidence` names the messages supporting each match.

Fixture validation checks structure, identity, evidence eligibility, and required scenario coverage. It does not execute a query handler. A production handler must later be tested against these literal expected answers, including the empty and undetermined results.
