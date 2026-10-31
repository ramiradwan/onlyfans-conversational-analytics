<!-- CODE-VERIFY: tools/optional_msix.py, tools/optional_engine_packaging.py, packaging/build-optional-engine.ps1, tests/test_optional_msix.py and tests/test_optional_engine_packaging.py own construction; installed qualification is separate. -->

# Optional executable package metadata

`tools/optional_msix.py` constructs an optional text-analysis package manifest, MakeAppx related-set mapping and Direct App Installer descriptor from an exact `EngineIdentity`. The optional executable has one fixed payload path, `worker/TextAnalysisWorker.exe`. The package has no independent activation entry.

Build the optional package and its bundle first. Build the main bundle with the optional bundle in the mapping's `ExternalPackages` section, using explicit bundle versions. Verify the generated main bundle's exact `OptionalBundle` identity and version.

`RelatedPackages` in the descriptor advertises the optional bundle without installing it with the main bundle; it does not replace bundle-level related-set metadata. See Microsoft's [related-set authoring](https://learn.microsoft.com/en-us/windows/msix/package/optional-packages) and [MakeAppx mapping example](https://learn.microsoft.com/en-us/archive/blogs/appinstaller/tooling-to-create-a-related-set).

The helper neither signs nor installs packages. Signature verification, immutable artifact inventories, Windows-derived identities, approved installed executable paths, on-demand installation, process containment, servicing and removal require separate installed-system qualification.

Windows owns executable replacement. Construction tests use synthetic bytes and establish metadata behavior only.

## Build a prebuilt Engine closure

`packaging/build-optional-engine.ps1` stages pinned prebuilt files, runs ordinary MakeAppx semantic validation for the application and bundle, and checks their exact unsigned payload before writing `producer-receipt.json`. The worker directory must contain exactly the flat binary closure declared by `worker-release.json`, plus that metadata file. Supported closure sizes follow the installed host's metadata policy; an instruction adapter also requires its complete CPU runtime and exactly one CPU backend. No worker compilation occurs.

```powershell
pwsh -File packaging/build-optional-engine.ps1 -BuildPython C:/build/python.exe `
  -Inputs D:/build/engine-inputs.json -InputsSha256 <sha256> -OutputRoot D:/build/engine-output
```

Add `-ValidateOnly` to validate inputs without creating output. Output must be a fresh absolute directory outside the source repository and declared worker directory, and cannot enclose any pinned input. The equivalent Python entry point is `python tools/optional_engine_packaging.py --inputs <absolute-path> --inputs-sha256 <sha256> --output <fresh-absolute-path> [--validate-only]`.

The closed input document uses schema `optional-engine-package-inputs/v1`. A file pin contains `path` (an absolute ordinary file), lowercase `sha256` and positive integer `size_bytes`.

| Field | Value |
| --- | --- |
| `package_profile` | Pin for a v2 direct related-package profile with the exact Core/Engine relationship and OS baseline. |
| `worker_release` | Pin for `worker_directory/worker-release.json`, using the existing closed Engine, artifact and adapter contracts. |
| `worker_directory` | Absolute directory containing the exact declared worker/runtime binaries and metadata. |
| `logo` | Pin for the PNG packaged as `Assets/StoreLogo.png`. |
| `notices` | Nonempty array of `{file, source}` entries: `file` is a canonical relative notice filename below `Notices/`; `source` is a file pin. |
| `makeappx` | Pin for the installed MakeAppx executable. Semantic validation cannot be disabled. |
| `build_receipts` | Array of file pins retained as build provenance; may be empty. Their contents grant no release or execution authority. |

The receipt records input pins, the original Engine identity, every payload hash/size, unsigned output hashes, the actual SDK commands and exact producer/helper source hashes. It makes no clean-commit claim. Metadata validation establishes declaration consistency, not the compiled worker's behavior. Signing, installation, related-package publication and analysis admission remain separate.
