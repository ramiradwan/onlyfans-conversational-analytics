# `bridge.localhost` browser-host spike

This directory is an isolated implementation spike for the ADR 0009 gate. It does not import or modify product code.

Playwright is intentionally a **spike-only development dependency** in this directory. The runner launches the system-installed stable browsers through Playwright channels `chrome` and `msedge`; it never installs or uses Playwright's bundled browser binaries.

Run from this directory with:

```powershell
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
npm install
npm run spike
```

`npm run spike` performs the port-owner checks, starts the loopback Brain stand-in, runs the named checks in fresh and then reused throwaway profiles for both browser channels, and rewrites `EVIDENCE.md`, `results.json`, and `request-log.json` from the observations.

The virtual CDP authenticator exercises Chromium's WebAuthn implementation but is not the physical Windows Hello platform authenticator. A manual real-authenticator confirmation remains a cutover requirement, as recorded in the evidence.
