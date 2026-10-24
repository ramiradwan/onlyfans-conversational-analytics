<!-- CODE-VERIFY: Check app/nlp/contracts.py, profiles.py, v2.py, adapter.py, app/analytics/runtime.py, app/analytics/nlp_literals.py, app/analytics/nlp_literal_read.py, tests/test_nlp_literal_publication.py, tests/test_nlp_literal_read.py, app/services/nlp_dependencies.py, tools/generate_nlp_contracts.py, shared/nlp/ and tests/test_nlp_contracts.py for contract facts. Native worker and installed delivery behavior require separate qualification. -->

# ADR 0057: Define optional local NLP computation and shared findings

- Status: Accepted; runtime activation requires separate qualification.
- Date: 2026-10-19
- Amended: 2026-10-23
- Extends: ADRs 0009, 0027 and 0056. ADR 0012 remains proposed.

## Decision

Permit one optional native computation worker under `native/nlp-worker/`. Brain remains the sole application writer and owns opt-in, authorization, account boundaries, source validation, scheduling and publication. The worker receives bounded text, opaque request identifiers and one pinned execution profile through private inherited pipes. It receives no database, credentials, network listener or publication authority. Conversation text never enters logs or temporary files.

The engine, CPU runtime and tokenizer remain outside the base installer. Direct MSIX uses a signed optional executable package in a qualified related set. Windows owns installation and servicing; the native host discovers exact approved identities and contains worker lifetime. Inno and Store retain base analytics and expose this optional capability as unavailable. There is no unmanaged executable fallback. A full-trust subprocess is fault isolation, not a security sandbox.

Model packs are authenticated immutable data, never executable extensions. Exact core/engine related-set compatibility and adapter artifact compatibility govern activation. Separate NLP trust metadata, hashes, sizes, durable publication sequence and withdrawal state govern acquisition. Analysis authorization authenticates neither executable nor data. Offline accepted installations may run under valid authorization; expired metadata blocks new activation. Learned withdrawal prevents execution.

An analysis profile binds message-local topic classification and concept extraction to one joint component or two serial components. Each component identifies its compiled adapter, engine, complete artifact closure, conditioning schema and preprocessing/decoding policies. Compatible data changes require a new profile; a different adapter requires a compatible signed engine. Packs cannot supply executable adapters or arbitrary task graphs.

The `nlp-profile.v3`, `nlp-result.v3` and `nlp-snapshot.v3` families permit a nonempty declared subset of supported tasks. Earlier families retain their required task sets. Results and comparisons cover exactly the declared tasks. `analytics-findings.v2` reports each supported task as declared or unavailable; an undeclared capability has no outcome or coverage. The worker protocol remains v2. Profile signatures use distinct version-bound protected types.

The findings read endpoint accepts paired `category_task` and `category_id` parameters for one enabled category in the saved profile. Filtered reads use `analytics-findings.v3` with an exact category echo and task availability. Category filtering narrows current evidence and conversation pages before pagination; summaries, coverage, denominators and previous-window comparisons retain their date, direction and explicit conversation population. Conversation entries count all eligible messages and are selected before the explicit conversation filter. Unfiltered reads retain their existing response versions.

V3 profile validation binds complete typed artifact closures and tokenizer formats; compiled adapters check each weight file's format and role during preparation. This permits components with different heads to share one encoder artifact identity, counted once in profile accounting. V2 retains homogeneous weight-format validation. Structural acceptance grants no execution readiness or release approval.

Stable topic and concept identifiers remain distinct from model labels. Findings describe text, including negated or quoted mentions; they do not establish participant preferences or intentions. Future tone uses a separate qualified task profile and does not alter baseline sentiment.

`app/nlp/contracts.py` retains v1 semantics. The profile, versioned result and adapter modules define the boundary; the generator owns schemas under `shared/nlp/`. Requests declare exact tasks and component/profile identities. Brain rejects mismatched, missing, extra or duplicate task results. Compatibility declarations and handshake results do not authenticate a release.

Each task has an independent outcome and coverage. Missing dependencies, uncertainty, failure and partial examination never become successful empty findings. Scores are optional, finite and explicitly typed; model scores are not measured accuracy. Evidence uses half-open original Unicode codepoint offsets. Deduplicate only identical source/concept/coordinate occurrences; repeated phrases at different positions remain distinct.

Shared findings belong in existing encrypted analytics storage through a versioned record boundary. Brain binds them to account, exact source version, profile and executed component. Retention, deletion and account barriers invalidate evidence. Snapshot identity includes source validity and per-task completion/availability so findings can publish without canonical edits. Reuse initially requires exact source and profile equality. Joint conditioning changes invalidate both tasks. Analytics reads never trigger synchronous inference.

One inference job runs across all profiles. The default resource policy permits at most two inference CPU threads including tokenizer pools. Components execute serially and unload between executions. Cancellation, exit and servicing drain or terminate the owned worker tree. Independent limits bound frames, batches, source text, windows, candidates and outputs. A qualified component may impose stricter limits than the protocol ceilings.

## Consequences

Brain-side normalization contracts in `app/nlp/normalization.py` separate source occurrence anchors from extraction provenance. Caller-retained account namespaces and NFC literal keys produce deterministic provisional node IDs; spelling equality does not approve semantic equivalence. Keys exceeding 256 code points are rejected. Versioned mapping and publication contracts have a 65,536-byte canonical bound. Publication manifests exclude derived group IDs; groups are publication-scoped references. Source-verified per-record derivations and immutable snapshot-owned literal publications use the existing analytics store. Empty or overlong literal keys retain explicit dispositions and extraction coverage; they never become truncated keys or negative findings. Publications authenticate exact record, namespace, type/taxonomy, policy and batch identities. Source/account erasure and snapshot/record reclamation cover their payload. These contracts implement no alias resolver, semantic equivalence or runtime activation. Existing v1/v2 worker messages and readers remain unchanged.

These contracts alone grant no runtime or release authority. Synthetic adapter conformance verifies the typed boundary. Optional runtime composition uses explicit configuration; default construction supplies no Engine provider or approved profile. Installed delivery, task quality and complete resource costs require separate qualification before activation. Existing baseline results and v1 records retain their meanings.

V2 descriptors declare bounded safe-integer resource costs independently of admission policy. The default local policy retains 250 MiB download, 512 MiB installed footprint including rollback, 512 MiB additional peak memory and two inference threads. A caller may explicitly supply a separately approved local policy; profile metadata cannot raise these limits. Structural validation still checks the complete artifact closure, aggregate installed accounting and component transition requirements. Resource admission grants neither release trust nor processing authorization. Qualification must measure complete runtime, transition, temporary and rollback costs before adopting a different policy.

`analytics-literals.v1` exposes distinct occurrence, message and conversation counts with exact source references and independent extraction/normalization coverage. Paging and selection pin the normalization publication identity; percentages remain unavailable.
