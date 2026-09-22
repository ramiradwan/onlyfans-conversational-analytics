<!-- CODE-VERIFY: Verify Windows requirements, installer behavior, provisioning prerequisites and steps, extension ordering, runtime paths, launcher behavior, and uninstall behavior against source and packaging configuration before editing. -->

# Install on Windows

OnlyFans Conversational Analytics installs per user. You do not need administrator rights, Python, Node.js, a repository checkout, or a provisioning environment variable.

## Requirements

- 64-bit Windows that can run x64 applications.
- A Chromium-based browser version 116 or later for the Agent extension.
- TCP port `17871` available on the local machine.
- A usable hardware-backed Microsoft Platform Crypto Provider for the installation signing key. Provisioning refuses software-only or unavailable installation-key providers.

Production packages carry the customer-facing secure-setup URL and hosted API origin as release-owned configuration. A customer does not set `LOCAL_PROVISIONING_HOSTED_ORIGIN`. Development and test runs may still use that variable when the checked-in development release configuration is intentionally blank.

## Download

Download the Windows installer and Agent extension bundle from the same published Windows package.

Before installing, [verify the downloaded files](verify-release.md) against the published SHA-256 digests.

## Install

Run `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe`.

The installer:

- installs the application under `%LOCALAPPDATA%\Programs\OnlyFans Conversational Analytics`;
- creates a Start Menu entry named **OnlyFans Conversational Analytics**;
- does not require administrator privileges.

## Add the Agent extension

The installer does not add the Agent to the browser. Install the Agent before first-run provisioning because the provisioning page uses it to detect the creator account.

Unpack `OnlyFans-Conversational-Analytics-Agent-<extension version>-chrome.zip` and add the unpacked directory to a Chromium-based browser as an extension.

See [Agent documentation](../extension/README.md) for its behavior and security boundaries.

## First run

Open **OnlyFans Conversational Analytics** from the Start Menu. The launcher starts Brain on the local machine and opens the provisioning page in your browser.

Complete the four provisioning steps:

1. Choose **Open secure setup**, sign in there, and obtain the setup code for this computer. Return to the desktop setup page, paste the complete code, and continue. The production handoff uses the `installation-claim-package:v2` / `installation-claim:v2` contract.
2. Confirm the creator account detected by the Agent.
3. Complete creator approval in secure setup, then return and choose **Check approval**.
4. Finish configuration.

After successful finalization, Brain exits provisioning mode, the launcher restarts it in runtime mode, and Bridge opens at `http://bridge.localhost:17871`.

If another process owns port `17871`, the launcher stops without terminating that process. Stop the conflicting process and start the application again.

## Local data

Runtime data is stored by default in `%LOCALAPPDATA%\OnlyFans Conversational Analytics`. This directory contains the local databases and runtime configuration.

To use another location, set `LOCAL_ANALYTICS_DATA_DIR` to an absolute path outside the installation directory before the first run.

## Uninstall

Uninstall through **Settings > Apps > Installed apps**, or run `unins000.exe` from the installation directory.

Uninstalling removes the application files and Start Menu entry. It leaves the runtime data directory in place so that uninstalling does not delete conversation data.

During bounded desktop setup the running process publishes its launcher handoff
credential as a current-user DPAPI-protected, owner-only file in the runtime data
directory. Relaunch first verifies the loopback listener image and user, then
requires the saved process ID to match before requesting a new short-lived browser
handoff. A stale, missing, corrupt, or different-process record fails closed. Normal
setup shutdown removes that process's credential; no browser session is extended.
