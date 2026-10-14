<!-- CODE-VERIFY: Verify smoke-harness prerequisites, parameters, exit codes, step outcomes, teardown behavior, PowerShell compatibility, and browser/hosted boundaries against tools/packaging-smoke/run.ps1 and its tests before editing. -->

# Run Windows packaging smoke acceptance

Use this procedure to verify a built Windows installer on a clean guest. The current harness proves the local packaging, launcher, listener-ownership, and teardown boundary. It does not automate full browser or hosted provisioning acceptance.

For normal installation and first-run provisioning, see [Install on Windows](install-windows.md).

## Prepare the guest

Use a Windows guest with:

- no Python installation;
- no Node.js installation;
- no checkout of this repository.

Restore the guest to a product-free checkpoint before each run. The command below uses the built-in Windows PowerShell host. The automated smoke tests also drive the same script with `pwsh.exe`; the harness does not reject PowerShell 7 by version.

A TPM-backed installation key is required for a separate complete setup run. The harness records the installer's hardware-key probe, but does not enroll the installation with the Provider.

## Run the harness

1. Copy the installer and its published SHA-256 digest to the guest.
2. Copy `tools/packaging-smoke/run.ps1` to a directory outside the installation tree.
3. Run the harness:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 `
  -ArtifactPath 'C:\path\to\OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe' `
  -PublishedSha256 '<published SHA-256>'
```

The harness installs into an isolated temporary location, starts the installed `Bridge.exe`, redirects runtime data to its temporary root, stops the desktop-owned process family, waits for port `17871` to be released, and uninstalls before it exits. Bridge owns the internal Brain process.

It writes `packaging-smoke-transcript.json` in the working directory unless `-TranscriptPath` specifies another path.

## Interpret the exit code

| Code | Meaning |
| --- | --- |
| `0` | Every recorded step passed. This is unreachable in the current harness because four provisioning-dependent steps are always recorded as blocked after the local smoke path. |
| `2` | The artifact file was missing. |
| `21` | Python was found on the clean guest. |
| `22` | Node.js was found on the clean guest. |
| `23` | A repository checkout was found in the inspected scope. |
| `24` | Port `17871` was already occupied. |
| `31` | The installer digest did not match the published value. |
| `32` | Installation failed. |
| `40` | Local smoke checks completed without a failing step, but one or more required provisioning steps were blocked. With the current harness, this is the expected successful local-smoke boundary. |
| `41` | An acceptance step failed. |

PowerShell parameter-binding failures, such as a malformed `-PublishedSha256` value, occur before the script's exit-code logic and are not represented by this table.

When `-InspectionRoot` is omitted, the repository check scans the system-drive root through four directory levels. Set `-InspectionRoot` when the clean-machine scope uses another root.

## What the harness proves

The transcript records non-secret evidence for these local steps:

1. Reject Python, Node.js, or a repository checkout in the inspected clean-machine scope.
2. Verify the installer SHA-256 digest.
3. Refuse to run when port `17871` is already occupied.
4. Install the verified artifact into an isolated temporary location.
5. Start the installed launcher.
6. Prove the responding provisioning listener on `127.0.0.1:17871` belongs to the launcher process family and returns a healthy response.
7. Stop the launcher-owned process family, confirm the listener port is released, uninstall, and remove the temporary run root.

The harness then records these steps as `blocked` by design:

- installation-key enrollment, because it requires the authenticated setup ceremony;
- native-to-browser handoff, because the harness does not drive the admitted desktop and Chrome surfaces;
- hosted sign-in, because it requires the installed browser task and a compatible Provider;
- enrollment completion, because it requires account approval and verified device-bound enrollment.

The harness never extracts session credentials, setup codes or enrollment material into its transcript.

## Full provisioning acceptance

`run.ps1` tears down the temporary installation immediately after recording the blocked boundary, so the browser and hosted provisioning sequence cannot be completed inside the current harness run.

To exercise installation enrollment, account association, passkeys, pairing and the account-bound extension connection, perform a separate normal installation and follow [Install on Windows](install-windows.md). Use the same installer and extension build. Verify both installation orders, Preview, Google and Microsoft sign-in, required approvals, optional Full activation, pause, restart, cancellation, wrong-profile handling and offline local access. Record transitions and customer actions as well as the terminal screen; keep identities, messages, cookies and codes out of evidence intended for sharing.

An unavailable or older Provider leaves the sign-in and approval checks incomplete. Local packaging checks and mocked HTTP tests cannot substitute for that installed-product qualification.
