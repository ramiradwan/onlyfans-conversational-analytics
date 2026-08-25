<!-- CODE-VERIFY: Verify helper message names, browser behavior, and build exclusion against current extension source before changing this status. -->

# Extension QA helpers

`qa/` contains old manual development helpers. They are not part of the built Agent artifact.

The helpers use legacy `_OF_FORWARDER_` and `_OF_BACKEND_` message names that are not part of the current production extension contract. Do not use these scripts as evidence that current Agent protocol or command behavior works.

Prefer the automated extension tests and the [capture E2E harness](../../tools/e2e-capture/README.md) for current behavior.

The extension build writes its production scripts, manifest, icons, notices, and build metadata to `dist/`; it does not copy this `qa/` directory.

Do not run these legacy helpers against a live account or production data. Update or remove the helper code in a separate code change if manual QA support is still required.
