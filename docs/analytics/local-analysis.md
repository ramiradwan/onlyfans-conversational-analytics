<!-- CODE-VERIFY: Check requirements.txt, packaging/pyinstaller/brain.spec, runtime-files.json, and model qualification evidence before changing delivery or resource claims. -->

# Local analysis and package limits

The base installer must provide deterministic conversation analytics without downloading ML dependencies or model weights. The current requirements contain no spaCy, Torch, transformer, or embedding runtime. The PyInstaller spec explicitly excludes Torch, transformers, and sentence-transformers. No trained model is selected by this policy.

## Choose the least costly sufficient method

Use deterministic code for direction, ordering, counts, response intervals, and observed outcomes. Evaluate explicit text or token rules for bounded language patterns. Evaluate a task-trained compact classifier when rules cannot meet the declared task quality.

Consider spaCy token matching or a compact trained pipeline only when token boundaries or linguistic features improve the actual task. Do not assume a general named-entity pipeline recognizes pricing discussions. Compare a sparse text classifier where appropriate. Use an embedding encoder for demonstrated semantic retrieval needs, not for counting messages.

An LLM requires evidence that smaller approaches cannot meet the task. It is never the default per-message processor. Natural-language question selection must first be tested with approved phrase patterns or a compact intent classifier. Preset questions remain usable without language interpretation.

A candidate report must name the task, language, artifact and license, training/evaluation data rights, quality results, abstention, runtime dependencies, and measured CPU/RAM/storage costs. General model benchmarks do not replace task evaluation. Pin weights, tokenizer, configuration, runtime, and taxonomy together.

## Optional downloads

Model weights and inference runtimes are separate costs. Deferring weights while bundling an unused large runtime does not meet the installer goal. Do not add optional ML packages to base requirements or unconditional application imports.

A future download flow must show purpose, total download bytes, installed bytes, temporary disk needs, supported languages, and hardware requirements before user approval. Declining, cancelling, going offline, or removing the pack must leave base analytics usable. Installed packs must run offline; model availability is not authority to run analysis.

Use application-owned, per-user, versioned packages. Verify an authenticated release manifest and each artifact's digest before activation. Pin supported OS/architecture and runtime compatibility. Allow only approved download origins; require bounded resumable transfers, timeouts, space checks, atomic activation, and cleanup of partial files.

Do not run arbitrary pip commands, downloaded setup scripts, custom model code, or unsafe deserializers on the customer's computer. Executable runtimes need their own signed distribution, dependency inventory, rollback, revocation, and packaging/security review. Do not add an inference subprocess without the architecture decision required by the single-writer runtime policy.

Local inference must never fall back to a hosted service or upload conversation content. Model traffic must not include messages, graph IDs, free-form questions, or account identifiers. Removal clears pack files and associated caches under the applicable deletion and retention rules without deleting canonical history.

## Laptop qualification

Target CPU-only Windows laptops with an SSD. Qualify an 8 GiB, four-core constrained profile and a 16 GiB, four-core reference profile. These are qualification targets, not advertised minimum requirements until tested. Record CPU instruction requirements; do not assume GPU, NPU, CUDA, or a particular vector instruction extension.

Initial acceptance budgets for a primary text-analysis pack are 250 MiB total download, 512 MiB installed size, and 512 MiB additional peak resident memory, including its runtime and dependencies. These are design limits, not measurements. Any exception requires a measured task benefit and an explicit product decision; an optional LLM must not silently raise the baseline.

Limit background analysis to one job and at most two CPU threads initially. Benchmark bounded batches, cold load, warm inference, cancellation, and sustained backlog processing while the UI and ingestion remain active. Pause or decline optional inference when memory or disk headroom is insufficient. Report unsupported hardware rather than repeatedly failing or switching to the cloud.

Measure compressed installer bytes, unpacked application bytes, shared runtime bytes, each language pack, peak temporary disk use, and process-tree peak memory separately. Count all transitive dependencies and installation overhead. Record base-versus-enabled deltas from the same build inputs; a model-card weight size is not an application-size measurement.

The [qualification procedure](qualification.md) defines the workload and result records. Downloads, pack management, and inference are not implemented by this specification.

## References

spaCy documents [rule-based matching](https://spacy.io/usage/rule-based-matching/) separately from [trained pipelines and installation](https://spacy.io/usage). These identify candidate techniques, not a selected OFCA dependency. Documentation checked 2026-09-18.
