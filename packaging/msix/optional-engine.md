<!-- CODE-VERIFY: tools/optional_msix.py and tests/test_optional_msix.py own metadata construction; installed qualification is separate. -->

# Optional executable package metadata

`tools/optional_msix.py` constructs an optional text-analysis package manifest, MakeAppx related-set mapping and Direct App Installer descriptor from an exact `EngineIdentity`. The optional executable has one fixed payload path, `worker/TextAnalysisWorker.exe`. The package has no independent activation entry.

Build the optional package and its bundle first. Build the main bundle with the optional bundle in the mapping's `ExternalPackages` section, using explicit bundle versions. Verify the generated main bundle's exact `OptionalBundle` identity and version.

`RelatedPackages` in the descriptor advertises the optional bundle without installing it with the main bundle; it does not replace bundle-level related-set metadata. See Microsoft's [related-set authoring](https://learn.microsoft.com/en-us/windows/msix/package/optional-packages) and [MakeAppx mapping example](https://learn.microsoft.com/en-us/archive/blogs/appinstaller/tooling-to-create-a-related-set).

The helper neither signs nor installs packages. Signature verification, immutable artifact inventories, Windows-derived identities, approved installed executable paths, on-demand installation, process containment, servicing and removal require separate installed-system qualification.

Windows owns executable replacement. Construction tests use synthetic bytes and establish metadata behavior only.
