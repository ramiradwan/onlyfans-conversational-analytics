# Native text-analysis probe

<!-- CODE-VERIFY: prepare.py, main.rs, measure.py, reference.py, normalize.py and Cargo.lock define source preparation, graph selection, runtime settings and measurement. -->

This isolated command-line probe runs a local four-graph text-analysis bundle. It has no application storage, acquisition or publication interface. It is not the production worker protocol.

Use a clean checkout of `https://github.com/codesoda/gliner2-rs` at `1bcf40e80c114ceb707d172b3efd28b577121b39`. Preserve its Apache-2.0 license and notices. Preparation copies that source into a new external build directory, removes Hub and binary-download dependencies, and leaves unrelated graph heads unloaded. The copied upstream source is not modified in place. The checked-in Cargo lock pins the resulting dependency closure.

```powershell
python tools/nlp-native-probe/prepare.py --source <pinned-source-checkout> --target <external-build-directory>
cargo build --release --locked -j 2 --manifest-path <external-build-directory>/Cargo.toml
```

The bundle requires `encoder.onnx`, `classifier.onnx`, `boundary_marginals.onnx`, `boundary_scorer.onnx`, `tokenizer.json` and compatible configuration. Supply a local x64 ONNX Runtime 1.28 DLL explicitly through `ORT_DYLIB_PATH`. No model or runtime acquisition occurs during execution. The process restricts its affinity to two permitted logical CPUs before runtime initialization. One shared ONNX CPU pool uses two intra-op threads, sequential sessions, disabled CPU arenas and disabled spinning. Tokenizer parallelism must be disabled before startup.

The recipe JSON contains ordered `topics` and `concepts` arrays with `id`, `label` and `definition`, plus `topic_threshold`, `mention_threshold`, `max_source_codepoints`, `max_source_words` and `max_input_tokens`. Each JSON input line contains `id`, `text` and `supported`. The probe emits findings, source codepoint coordinates, elapsed times and explicit unsupported/partial outcomes. Oversized input is withheld; this probe implements no long-message windowing or complete-coverage guarantee. Inputs must already be bounded by the caller.

`measure.py` requires development-only `psutil`. Run it through the repository's host workload guard with explicit executable, model, runtime, recipe, inputs and a new output directory. Supply a provenance JSON listing frozen file paths and SHA256 hashes, plus an explicit `reference_identity` containing the reference manifest entry name and SHA256. It validates those hashes before launch, records process-tree working-set samples during loading and inference, queries the held Windows process handle after exit, and records cumulative in-worker lifetime peaks, private bytes, thread CPU samples and artifact/input hashes. A memory result is separate from model quality, coordinate correctness, deployment qualification and constrained-device behavior. Total process thread count does not establish how many threads performed inference.

`reference.py` runs the pinned upstream Python implementation in a separate development environment with local files, offline network enforcement, two CPU threads and eager FP32 execution. It records raw and normalized outputs, encoded token counts and source identities. Use matching recipes and eligibility inputs to compare FP32 native conversion/decoding first, then quantify changes from quantized graphs separately. Reference execution is not the delivered runtime. Failed invocations write a fixed-code receipt in newly created output directories.

`normalize.py` defines `inclusive-threshold.v1`, a separate diagnostic output policy. It keeps findings whose uncalibrated score is greater than or equal to the corresponding declared threshold, permits empty assessed findings, and preserves withheld partial or unsupported outcomes. Raw upstream outputs remain available for parity comparison. The policy does not modify numerical thresholds, model weights or label definitions.

Pure validation and output-policy checks are collected by `pytest tests/test_nlp_native_probe.py`. Importing those checks does not load the optional runtime or machine-learning dependencies.
