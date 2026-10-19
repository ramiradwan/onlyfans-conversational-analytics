<!-- CODE-VERIFY: Check app/nlp/contracts.py, tools/generate_nlp_contracts.py, shared/nlp/v1/ and tests/test_nlp_contracts.py for contract facts; native worker and installed delivery behavior require separate qualification. -->

# ADR 0057: Define optional local NLP computation and shared findings

- Status: Accepted; runtime integration requires separate qualification.
- Date: 2026-10-19
- Extends: ADRs 0009, 0027 and 0056. ADR 0012 remains proposed.

## Decision

Permit one optional native computation worker under `native/nlp-worker/`. Brain remains the sole application writer and owns opt-in, authorization, account boundaries, source validation, scheduling and publication. The worker receives bounded text, opaque request identifiers and one pinned execution profile through private inherited pipes. It receives no database, credentials, network listener or publication authority. Conversation text never enters logs or temporary files.

The engine, CPU runtime and tokenizer remain outside the base installer. Direct MSIX uses a signed optional executable package in a qualified related set. Windows owns installation and servicing; the native host discovers exact approved identities and contains worker lifetime. Inno and Store retain base analytics and expose this optional capability as unavailable. There is no unmanaged executable fallback. A full-trust subprocess is fault isolation, not a security sandbox.

Model packs are authenticated immutable data, never executable extensions. Exact core/engine related-set compatibility and pack engine allow-lists govern activation. Separate NLP trust metadata, hashes, sizes, durable publication sequence and withdrawal state govern acquisition. Analysis authorization authenticates neither executable nor data. Offline accepted installations may run under valid authorization; expired metadata blocks new activation. Learned withdrawal prevents execution.

V1 uses one fixed message-local topics-and-mentions recipe. Stable topic and concept identifiers remain distinct. Findings describe text, including negated or quoted mentions; they do not establish participant preferences or intentions. Future tone uses a separate qualified recipe and does not alter baseline sentiment or closed enums.

Typed contracts live in `app/nlp/contracts.py`; its generator owns `shared/nlp/v1/`. Each task has an independent outcome and coverage. Missing dependencies, uncertainty, failure and partial examination never become successful empty findings. Evidence uses half-open original Unicode codepoint offsets. Overlapping-window duplicates share concept and coordinates; repeated phrases at different positions remain distinct.

Shared findings are derived records in existing encrypted analytics storage. Brain binds them to account, exact source version, execution identity and source validity. Retention, deletion and account barriers invalidate evidence. Frozen enrichment snapshots include canonical, completion and availability revisions, execution identity, findings digest and source-validity digest. Currentness and generation reuse must include this identity so newly completed findings publish without canonical edits. Analytics reads never trigger synchronous inference.

One inference job and one loaded profile use at most two inference CPU threads including tokenizer pools. Cancellation, exit and servicing drain or terminate the owned worker tree. Independent limits bound frames, batches, source text, windows, candidates and outputs. A qualified recipe may impose stricter limits than the protocol ceilings.

## Consequences

Contracts enable no inference, acquisition or user interface. Native CPU execution and installed optional-package behavior require separate qualification before runtime integration. Schema tests cannot qualify either behavior.

Optional artifacts remain bounded to 250 MiB download and 512 MiB installed footprint. Additional peak resident memory is bounded to 512 MiB. Runtime dependencies and retained rollback artifacts count toward these limits; temporary disk is measured separately.
