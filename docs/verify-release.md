<!-- CODE-VERIFY: Verify published artifact names, Authenticode requirements, timestamp verification, and digest generation against the Windows package workflow and packaging scripts before editing. -->

# Verify a release

Use these checks to confirm that downloaded Windows package files match the artifact produced by the tagged package workflow.

## Published files

The tagged Windows package workflow publishes an artifact containing:

- `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe` — the installer;
- `OnlyFans-Conversational-Analytics-Agent-<extension version>-chrome.zip` — the Agent extension bundle;
- `sha256sums.txt` — SHA-256 digests for the published files.

## Verify the installer signature

The tagged Windows package workflow signs the installer with Authenticode and requires a valid timestamped signature before publishing the signed package artifact.

In PowerShell, inspect the installer signature:

```powershell
Get-AuthenticodeSignature .\OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe
```

The signature status should be `Valid`. The Agent ZIP does not carry an embedded Authenticode signature, so verify it with the published digest.

## Verify the published digest

Calculate the SHA-256 digest of a downloaded file:

```powershell
Get-FileHash -Algorithm SHA256 .\OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe
```

Compare the result with the entry for the same filename in `sha256sums.txt`. Repeat the check for the Agent bundle if you downloaded it.

The signing job recomputes `sha256sums.txt` after signing the installer, so the published digest covers the signed installer bytes.
