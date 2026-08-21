# Install and run on Windows

OnlyFans Conversational Analytics ships as a per-user Windows installer. Installation requires no
administrator rights, no Python or Node.js installation, and no repository checkout.

## Requirements

- 64-bit Windows on an x64-compatible processor. The installer declares `x64compatible`, so it also
  accepts an ARM64 system that runs x64 binaries.
- A Chromium-based browser at version 116 or later, for the Agent extension.
- Loopback TCP port 17871 free. Brain binds `127.0.0.1:17871` and serves Bridge at the fixed origin
  `http://bridge.localhost:17871`.

## Release artifacts

A release publishes three files from one directory:

- `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe` — the installer, where `<version>` is
  the Brain version declared in `app/core/config.py`.
- `OnlyFans-Conversational-Analytics-Agent-<extension version>-chrome.zip` — the Agent browser
  extension, packed from the same build.
- `sha256sums.txt` — one line per published file, in the form `<sha256> *<relative/path>`.

A `sha256sums.txt` covers the files that sit beside it. The published one covers the installer and
the Agent bundle; the installed program directory contains its own, covering the installed files.

## Verify the download

Released artifacts are not signed. There is no code signature, no publisher identity, and no
timestamp countersignature on the installer or on any file it contains. Expect the following:

- Microsoft Defender SmartScreen shows the "Windows protected your PC" dialog when the installer
  starts, because the application is unrecognized and unsigned. Continuing requires **More info**
  and then **Run anyway**.
- Windows reports no publisher for the installer.
- A browser may warn while downloading an unsigned executable.

No administrator prompt appears at any point; the installer requests the lowest privileges.

Because no signature exists, comparing digests is the available integrity check. Compute the
installer's SHA-256 and compare it with the entry for that filename in the `sha256sums.txt`
published with the release:

```powershell
Get-FileHash -Algorithm SHA256 .\OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe
```

The same file records the digest of the Agent bundle. A digest match establishes that a copy is
byte-identical to the published bytes. It does not establish who produced them.

## Install

Run the installer. It:

- installs under `%LOCALAPPDATA%\Programs\OnlyFans Conversational Analytics`;
- creates one Start Menu entry, **OnlyFans Conversational Analytics**, that runs `Brain.exe`;
- writes nothing outside the installation directory.

The installer accepts the standard Inno Setup command-line switches, including `/SILENT`,
`/SUPPRESSMSGBOXES`, and `/DIR="<path>"` for an unattended installation into a chosen directory.

## First run

Start **OnlyFans Conversational Analytics** from the Start Menu. `Brain.exe` is a launcher. It
inspects the listener on port 17871, starts Brain as a background process when the port is free, and
opens the system browser at `http://bridge.localhost:17871` through a single-use handoff code.

Until the runtime is configured, the launcher opens the configuration page instead of Bridge. The
page has four steps:

1. **Register this installation** — paste the installation package into the form.
2. **Confirm the signed-in creator account** — the page reads the account the Agent detects and
   associates it with this installation.
3. **Acquire association approval**.
4. **Finish configuration** — writes `runtime.env` into the data directory.

Brain then restarts into runtime mode, and the launcher opens Bridge.

If a process other than the installed `Brain.exe` running under the current user owns port 17871,
the launcher stops with a message and does not terminate that process. Stop the conflicting process
and start the launcher again.

## Agent browser extension

The Agent is a separate Chrome MV3 extension named `OnlyFans Conversational Analytics Agent`. The
installer does not install it into the browser: the packaging step stages the built extension beside
the program files so that the packaging policy covers it, the installer excludes that directory, and
the release publishes it separately as
`OnlyFans-Conversational-Analytics-Agent-<extension version>-chrome.zip`. Unpack that archive and
add the resulting directory to the browser.

The extension's `manifest.json` pins a public `key`, so the extension ID is fixed rather than
derived from the installation path. Its content security policy permits connections only to
`http://bridge.localhost:17871` and `ws://bridge.localhost:17871`, and its host permissions are
limited to `https://onlyfans.com/*` and `http://bridge.localhost/*`. It requires Chrome 116 or later.

## Local data

Per-user runtime state lives outside the installation directory, in
`%LOCALAPPDATA%\OnlyFans Conversational Analytics`. The directory is created with owner-only
permissions and holds:

- `runtime.env` — the runtime configuration written during the configuration sequence, including the
  absolute database paths;
- `canonical.sqlite3` — the authoritative conversation store;
- `auth.sqlite3`, `projections.sqlite3`, and `analytics-projections.sqlite3`.

`LOCAL_ANALYTICS_DATA_DIR` overrides the location. The value must be an absolute path and must not
resolve inside the installed application. Database paths are recorded in `runtime.env` when the
configuration is created, so set the override before the first run rather than after it.

## Uninstall

Uninstall through **Settings > Apps > Installed apps**, or run `unins000.exe` in the installation
directory. Uninstalling removes the installation directory and the Start Menu entry.

The per-user data directory is deliberately left in place, because it holds the authoritative
database and remains available for viewing, export, backup, or recovery. Delete
`%LOCALAPPDATA%\OnlyFans Conversational Analytics` yourself when the data it holds is no longer
wanted.

## Build the installer from source

`packaging/build-windows.ps1` produces the installer. It requires:

- a build interpreter in an isolated environment with `requirements.txt` and
  `packaging/requirements-build.txt` installed — not the repository's `.venv`;
- Node.js and npm, unless `-SkipAssetBuild` is passed, because the script builds the Bridge frontend
  and the Agent artifact;
- the Inno Setup 6 compiler, resolved from `-InnoSetupCompiler`, the `INNO_SETUP_COMPILER` or `ISCC`
  environment variable, `PATH`, or the standard installation locations;
- an output directory outside the repository that does not already exist.

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements.txt -r packaging/requirements-build.txt
.\packaging\build-windows.ps1 -BuildPython .\.build-venv\Scripts\python.exe
```

The script freezes Brain with PyInstaller, embeds the Agent extension identity, stages the runtime
files declared in `packaging/runtime-files.json`, writes `release-manifest.json`, verifies the staged
tree against that policy, and writes the installed program's `sha256sums.txt` over the files the
installer places. It then compiles the Inno Setup script, packs the staged `Agent` directory into the
Agent bundle, and writes the published `sha256sums.txt` over the installer and that bundle. Both
digest files are written by `packaging/write-digests.ps1`. The three published files land in
`installer\` under the output root. The script performs no signing: it handles no signing material,
accepts none, and invokes no signing tool.

To exercise a built installer end to end on a clean guest, see
[the acceptance sequence](installation-and-acceptance.md).
