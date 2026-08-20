# Install and run the product acceptance sequence

OnlyFans Conversational Analytics runs locally on the creator's Windows account. Brain hosts the local Bridge at `http://bridge.localhost:17871`, and the Agent is the browser extension that connects the signed-in creator account to that local runtime.

## Install

Use a Windows guest with a TPM-capable configuration and no Python installation, Node installation, or product repository checkout. Restore the guest to its product-free checkpoint before each run.

1. Obtain the published release artifact and its SHA-256 digest from the same release source.
2. Install the artifact using its supplied installer. Do not add files to the installation directory or edit generated application state.
3. Copy `tools/packaging-smoke/run.ps1` into the guest as acceptance tooling. It is not part of the installed product.
4. Run the script from a directory outside the installation tree, supplying the installed artifact path, its published SHA-256 digest, and the installed launcher path.

```powershell
pwsh -NoProfile -File .\run.ps1 `
  -ArtifactPath 'C:\path\to\installed\Brain.exe' `
  -PublishedSha256 '<published SHA-256>' `
  -LauncherPath 'C:\path\to\installed\Brain.exe'
```

The script writes `packaging-smoke-transcript.json` beside the command's working directory unless `-TranscriptPath` specifies another location. It exits with `21` when Python is detected, `22` when Node is detected, and `23` when a repository checkout is found in an inspected root. A blocked sequence exits `40`; a failed acceptance step exits `41`.

## Acceptance sequence

The transcript records an outcome and non-secret evidence for each action:

1. Verify the installed artifact SHA-256 against the published digest.
2. Open Bridge through the installed launcher.
3. Confirm the provisioning listener answers its local health endpoint.
4. Confirm installation-key readiness through an available non-secret product signal.
5. Complete the launcher-to-browser provisioning handoff.
6. Paste the installation claim into the provisioning page.
7. Let Brain consume the claim through the hosted provisioning service.

The final four actions require browser-bound state, a real claim, and—in the claim-consumption case—a reachable hosted provisioning service. When those inputs are absent, the script records `blocked` with the reason. It never treats a blocked action as a pass and never places claim material in the command line or transcript.

After claim consumption, continue in Bridge: confirm the detected creator account, complete the hosted approval, enroll and sign in with the local WebAuthn credential, request Agent pairing, and confirm the Agent connects for that exact account.

## Artifact boundary

The acceptance script compares a real installed artifact with a published digest. Packaging-policy tests inspect staged contents and reject per-user material; they do not construct a release artifact for a hash comparison. A production-artifact hash-equality test belongs with the installer assembly input when that input exists.
