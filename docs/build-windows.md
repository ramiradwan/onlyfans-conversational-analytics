<!-- CODE-VERIFY: Verify build prerequisites, PowerShell parameters, output layout, artifact names, signing behavior, and digest generation against packaging scripts and the Windows package workflow before editing. -->

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
