<!-- CODE-VERIFY: Verify build prerequisites, PowerShell parameters, output layout, artifact names, signing behavior, and digest generation against packaging scripts and the Windows package workflow before editing. Verify staged material admission against tools/packaging_policy.py, packaging/runtime-files.json, and tests/test_packaging_policy.py. -->

# Build Windows release artifacts

`packaging/build-windows.ps1` builds the installer, Agent bundle, and release digests.

## Requirements

- An isolated Python environment with `requirements.txt` and `packaging/requirements-build.txt`. Build the local SQLCipher wheel before installing dependencies.
- Node.js and npm, unless using `-SkipAssetBuild`.
- Inno Setup 6.
- A new output directory outside the repository.

The script finds Inno Setup from `-InnoSetupCompiler`, `INNO_SETUP_COMPILER`, `ISCC`, `PATH`, or standard install locations.

## Build

```powershell
python -m venv .build-venv
.\packaging\sqlcipher\build-fixed-wheel.ps1 -BuildPython python.exe -Wheelhouse "$env:TEMP\ofca-sqlcipher-wheelhouse"
.\.build-venv\Scripts\python.exe -m pip install --find-links "$env:TEMP\ofca-sqlcipher-wheelhouse" -r requirements.txt -r packaging/requirements-build.txt
.\packaging\build-windows.ps1 -BuildPython .\.build-venv\Scripts\python.exe
```

## Output

The script stages runtime files, freezes Brain, builds Agent, compiles the installer, and writes SHA-256 digests. The installer is under `installer\` in the output root.

The local installer is unsigned. The release workflow signs and timestamps it, verifies Authenticode, and recomputes digests. The workflow accepts only immutable `v*` tags.

See [Windows acceptance](installation-and-acceptance.md) to test the installer on a clean guest.

## Staged material checks

The [runtime policy](../packaging/runtime-files.json) rejects per-user claims, secrets, tokens and profile paths. A material rule may declare `reviewed_synthetic_files`, mapping exact relative paths to SHA-256 digests of reviewed public fixtures. Only matching bytes at the declared path are admitted for that rule's payload scan. Path checks, other material rules and the contract integrity checks still apply.

The three initial-handoff fixtures contain public action digests under `issue-installation-claim`, which also match the claim detector. Their admission is bound to the immutable contract bytes. Changed bytes and copied or renamed files receive the normal material scan. Invalid paths, wildcards and malformed digests invalidate the declaration.

After an approved contract update, verify the generated snapshot and update the policy's manifest, consumer-pin and support-file digest anchors. Review any synthetic fixture admission separately; never admit a folder or change a detector to hide a fixture match. Run `python -m pytest tests/test_packaging_policy.py tests/test_build_windows.py` before committing the policy.
