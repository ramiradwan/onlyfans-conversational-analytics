# Optional local text-analysis contracts

<!-- CODE-VERIFY: app/nlp/contracts.py, tools/generate_nlp_contracts.py and tests/test_nlp_contracts.py own schema generation, semantic validation and fixture checks. -->

`app/nlp/contracts.py` owns the typed contracts. Run `python tools/generate_nlp_contracts.py` to regenerate the JSON Schemas. Do not edit generated schemas by hand. The fixtures contain synthetic mechanical examples, not qualified predictions or category enablement.

Frames are UTF-8 JSON payloads, at most 1 MiB before decoding. The private-pipe transport must enforce this bound before allocating or reading a declared frame. Each batch contains at most 16 unique opaque item identifiers. Requests carry no account identifier, storage location or executable path. Byte framing and process launch are separate runtime responsibilities.

Frame boundaries raise fixed-code `NlpBoundaryError` diagnostics and suppress underlying validation chains. Raw typed-model validation errors may include input values; runtime callers use the frame boundaries.

Schemas enforce structural limits. Brain additionally validates the exact request identity, execution profile, item set, source codepoint lengths, coverage partitions, task separation and span containment with `validate_response`. Callers must resolve the current canonical source version, authorization, qualified category allow-list and active execution profile before publication. No worker result grants publication authority.

Span coordinates are half-open Unicode codepoint offsets into the original message. Original slices supply evidence; decoder text cannot become evidence. Occurrence identity is the source binding, concept identifier and coordinates. Overlapping-window duplicates at identical coordinates collapse; repeated text at different coordinates remains distinct. Browser UTF-16 conversion belongs at presentation.

Topic and mention outcomes are independent. A partial result can retain examined findings but cannot establish whole-message absence. Raw scores are model scores, not measured accuracy. V1 reports `not_calibrated`; category quality approval belongs to Brain-validated authenticated metadata and cannot be asserted by the worker. Taxonomy labels, definitions, thresholds, windowing and decoding settings are pinned through execution digests. V1 uses one fixed joint topics-and-mentions recipe; future tone requires a separate contract.

The pack descriptor lists exact compatible engine identities and approved stable identifiers. It is a compatibility value, not authenticated release metadata or an installer. Native discovery verifies Windows-derived identity and approved executable location. Deployment outcomes carry bounded refusal, cancellation and restart states without caller-selected URLs or paths.

Frozen snapshot identity includes canonical, completion and availability revisions. Changed findings or availability can invalidate currentness without a canonical edit. Records retain source references rather than message bodies. Persisted evidence remains subject to canonical source validity, account barriers, deletion and retention.
