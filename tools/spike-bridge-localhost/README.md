<!-- CODE-VERIFY: Verify package scripts, browser channels, generated evidence files, and remaining manual checks against the spike source before editing. -->

# `bridge.localhost` browser-host spike

This directory is an isolated implementation spike for the ADR 0009 browser-host decision. It does not import or modify product code.

The spike uses system-installed Chrome and Edge through Playwright channels. It does not use Playwright's downloaded browser binaries.

## Run the spike

From this directory:

```powershell
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
npm ci
npm run spike
```

The run checks browser behavior against the local stand-in and rewrites `EVIDENCE.md`, `results.json`, and `request-log.json` from the observations.

The virtual WebAuthn authenticator exercises Chromium's WebAuthn flow but is not a physical Windows Hello authenticator. Any remaining manual real-authenticator requirement is recorded in `EVIDENCE.md`.
