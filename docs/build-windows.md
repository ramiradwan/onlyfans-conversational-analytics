<!-- CODE-VERIFY: Verify build prerequisites, PowerShell parameters, output layout, artifact names, signing behavior, and digest generation against packaging scripts and the Windows package workflow before editing. -->

# Build Windows release artifacts

Use `packaging/build-windows.ps1` to build the Windows installer, Agent extension bundle, and release digests.

## Requirements

- An isolated Python environment with `requirements.txt` and `packaging/requirements-build.txt` installed. Do not use the repository `.venv` for packaging.
- Node.js and npm unless you pass `-SkipAssetBuild`.
- Inno Setup 6.
- An output directory outside the repository that does not already exist.

The build script can find Inno Setup from `-InnoSetupCompiler`, the `INNO_SETUP_COMPILER` or `ISCC` environment variable, `PATH`, or the standard installation locations.

## Build

From the repository root:

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements.txt -r packaging/requirements-build.txt
.\packaging\build-windows.ps1 -BuildPython .\.build-venv\Scripts\python.exe
```

## Output

The build stages and verifies the runtime files, freezes Brain, builds the Agent bundle, compiles the installer, and writes SHA-256 digest files. The package is written under `installer\` in the output root.

`build-windows.ps1` produces an unsigned installer. For tagged releases, the Windows package workflow signs the installer, verifies that its Authenticode signature is valid and timestamped, then recomputes the published digests.

To test a built installer on a clean Windows guest, see [Run Windows acceptance](installation-and-acceptance.md).
