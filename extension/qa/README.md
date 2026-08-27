<!-- CODE-VERIFY: Verify helper message names, browser behavior, and build exclusion against current extension source before editing. -->

# Extension QA helpers

`qa/` contains manual development helpers. They are excluded from the built Agent artifact.

The helpers exercise the development-only `_OF_FORWARDER_` and `_OF_BACKEND_`
message channels. Automated protocol and command tests define the supported Agent behavior.

Prefer the automated extension tests and the [capture E2E harness](../../tools/e2e-capture/README.md) for current behavior.

The extension build writes its production scripts, manifest, icons, notices, and build metadata to `dist/`; it does not copy this `qa/` directory.

Run these helpers only with synthetic data.
