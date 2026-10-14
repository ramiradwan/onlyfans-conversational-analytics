<!-- CODE-VERIFY: check.mjs check.test.mjs ../../.github/workflows/windows-package.yml -->

# Verify the desktop release credential

Select `r2-credential-check` in the existing Windows package workflow. Dispatch against a `vcredential-check-*` tag with its exact commit as `product_revision`. The release environment's existing `v*` tag restriction remains in force. Required Legal inputs are unused in this mode; use `not-used-credential-check` rather than release coordinates.

The preparation job installs the locked SDK and runs safety checks without release credentials. The release-environment job receives only the two R2 secrets and configured bucket and endpoint. It creates 4,096 random bytes under `_credential-checks/github-release/<run-id>/<nonce>.bin` in `dipsy-desktop-releases-prod`. The conditional upload refuses overwrites. It verifies the retrieved length and SHA-256, deletes the ownership-checked object, and requires HEAD and GET to return 404.

Local intent is flushed before any request. Cleanup runs in the check's `finally` block and again in an `always()` workflow step. It refuses other keys, buckets, runs and objects without matching ownership metadata and bytes. Verification or cleanup failure fails the run. The retained receipt contains no credentials.

This mode skips package builds and signing. It does not upload installers, promote an App Installer descriptor, deploy the Worker or authorize a customer release.

Run local safety checks with `npm ci --ignore-scripts --no-audit --no-fund` and `npm test` in this directory. Those checks do not verify the real release credential; only a successful hosted round trip does.
