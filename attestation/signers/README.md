# Attestation signer keys and lifecycle

<!-- CODE-VERIFY: Check .github/workflows/engineering-attestation.yml and tools/engineering_attestation.py before changing signer identity, environment names, key paths, credentials, or lifecycle procedures. -->

The protected producer uses Ed25519 signer ID `product-engineering-attestation-ed25519-v1`. The production private key is never committed. Its matching public key will be committed as `product-engineering-attestation-ed25519-v1.pem` only after the producer control baseline and verifier prerequisites are active.

## Protected configuration

Configure the `engineering-attestation-production` environment only after the producer and verifier owners independently compare the signer ID and fingerprint. Its deployment-branch policy must allow the exact `main` branch only, with no tag or other branch pattern. This platform gate is what prevents a modified workflow on an unmerged ref from receiving protected secrets; the producer's current-`main` checks are an additional fail-closed check, not a substitute. The fingerprint format is `sha256:<lowercase SHA-256 of DER SubjectPublicKeyInfo>`.

Repository variable:

- `PRODUCER_CONTROL_BASELINE_SHA`

Environment variables:

- `ENGINEERING_ATTESTATION_SIGNER_ID`
- `ENGINEERING_ATTESTATION_SPKI_SHA256`

Environment secrets:

- `ENGINEERING_ATTESTATION_PRIVATE_KEY_B64`, standard Base64 of the unencrypted Ed25519 private PEM;
- `REVIEW_REPOSITORY`;
- `REVIEW_DEFAULT_BRANCH`;
- `REVIEW_PROJECTION_PATH`;
- `REVIEW_PROJECTION_DIGEST_PATH`;
- `REVIEW_APP_ID`;
- `REVIEW_INSTALLATION_ID`;
- `REVIEW_BOT_USER_ID`;
- `REVIEW_REPOSITORY_ID`;
- `REVIEW_APP_PRIVATE_KEY_B64`, standard Base64 of the dedicated review GitHub App private PEM.

The protected review configuration values have no tracked defaults. The workflow verifies that the resolved repository address and default branch still match the protected configuration, then independently verifies the numeric App, installation, bot-user, and repository identities. A rename, transfer, or default-branch change stops the producer until the protected configuration is deliberately reviewed and updated.

The App must be installed on one selected review repository with only metadata read, contents read/write, and pull-request read/write. It cannot update verifier trust settings and must not be a ruleset bypass actor.

Environment approval is a single-approver gate. It is not two-person control and does not constitute independent review.

A `MATCH` result and an accepted evidence record establish producer identity and exact artifact provenance. They do not authorize publication or release.

## Activate the producer controls

Leave `PRODUCER_CONTROL_BASELINE_SHA` unset until this producer is squash-merged to `main`. After merge, set it to that exact squash-merge SHA. The workflow reads the variable at runtime and fails closed while it is unset. Both the producer workflow commit and every attested Product source commit must be strict descendants of the baseline; equality is rejected. The earliest attestable source is therefore the next `main` commit after the producer baseline, never the baseline commit itself.

Before production-key activation, all of these Product-side controls must be active:

- protect `main` with pull-request-only changes, the required Product CI checks, signed commits, linear history, and deletion and force-push prevention;
- authorize creation of `v*` tags only for the designated release actor;
- prevent update, deletion, and non-fast-forward movement of every `v*` tag with a separate active ruleset that has no bypass actors;
- create the `engineering-attestation-production` environment with the exact-`main` deployment policy and single-approver gate above, without adding a production private key yet.

Query the repository after activation and verify the effective rules, required check names and sources, tag patterns, exact-`main` environment deployment policy, and zero-bypass immutable-tag rule. After the producer PR is squash-merged, set and re-read the baseline variable before any key or App credential is provisioned. Treat any later baseline change as a trust-root change requiring deliberate review; never lower or replace it to admit another package.

Do not represent multiple checkpoints performed by the same authorized operator as independent approval. Enable a separate required approval only when a second trusted reviewer is available.

## Establish the first key

1. Squash-merge the producer code, set the merge SHA as the v1 baseline, and re-read the runtime variable.
2. Confirm the verifier, vectors, signer registry, evidence intake, and no-bypass gate are active.
3. Generate an Ed25519 key offline.
4. Commit only the public PEM at the path above.
5. Deliver the public PEM to the verifier owner through an independent channel for its immutable registry entry.
6. Compare the signer ID and SPKI fingerprint on both sides.
7. Configure the protected private-key secret and App credentials.
8. Qualify a fresh post-baseline tagged package and complete a clean-install rehearsal on a machine with no repository checkout before the first attestation.

## Rotate a key

1. Stop new signing while the transition is prepared.
2. Generate a new offline key and use a new versioned signer ID.
3. Commit the new Product public PEM and independently add its immutable verifier registry record.
4. Compare the new signer identity and SPKI fingerprint on both sides.
5. Activate the new signer in both protected environments.
6. Mark the old registry entry retired and remove its private key from active Product configuration.

Keep the retired public key and historical evidence. Never reuse a signer ID, rewrite a registry key, or let Product automation rotate the verifier trust root.

## Respond to suspected compromise

1. Disable the Product attestation environment immediately.
2. Remove the suspected key from the active producer and verifier environments.
3. Mark its verifier registry entry compromised and record the response reference.
4. Review every potentially affected attestation.
5. Generate a new offline key with a new signer ID and repeat independent activation.

Do not delete or rewrite historical evidence. Compromise status adds a review warning; it does not change what earlier signed bytes contain.
