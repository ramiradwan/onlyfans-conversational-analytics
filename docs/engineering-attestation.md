# Attest a released Chrome package

<!-- CODE-VERIFY: Check .github/workflows/engineering-attestation.yml, tools/engineering_attestation.py, packaging/new-agent-bundle.ps1, and attestation/signers/README.md before changing workflow inputs, trust values, artifact rules, or activation steps. -->

The engineering-attestation workflow signs the exact Chrome ZIP from a successful Windows-package run and privately submits the same bytes for review. Version 1 proves producer identity, artifact provenance, and binding to a signed review-state projection. The v1 wire member carrying that projection is `legal_projection`.

It does not detect changes to permissions, hosts, network destinations, storage, retained authentication values, data categories, write behavior, retention, deletion, or telemetry. Those changes require human review before the `engineering_facts` profile is versioned.

A `MATCH` result and an accepted evidence record establish producer identity and exact artifact provenance. They do not authorize publication or release.

## Before configuring a production key

Do not configure a production private key until the review-side consumer has supplied and activated all of the following:

- a validated projection snapshot;
- strict signer-ID binding and duplicate-key rejection;
- shared conformance vectors;
- a historical signer registry;
- immutable four-file evidence intake;
- a no-bypass evidence ruleset with the exact scope and aggregate `MATCH` checks.

Leave `PRODUCER_CONTROL_BASELINE_SHA` unset until this producer is squash-merged to `main`, then set it to that merge SHA. The workflow reads it at runtime and fails closed while it is unset. The workflow and every attested Product source commit must be strict descendants; the baseline commit itself cannot be attested.

The required Product environment, variables, and secrets are listed in [the signer key guide](../attestation/signers/README.md).

## Run the workflow

Use a fresh candidate from a commit after the producer control baseline and a new immutable `v*` tag. Do not reuse a pre-control package.

1. Dispatch `.github/workflows/windows-package.yml` from the release tag itself: select the `v*` tag under **Use workflow from**, and pass `product_revision` as the exact 40-character commit that tag names. The workflow refuses any other dispatch ref before it retrieves a document or builds anything, and the attestation refuses a run whose head branch is not the release tag.
2. Confirm the source commit has a completed, successful Product CI `push` run on `main` with the exact three required jobs, and that the Windows-package run is green. Pull-request CI does not qualify a source package.
3. Complete a clean-install rehearsal for the exact packaged installer on a machine with no repository checkout.
4. Obtain the reviewed projection source commit and its canonical SHA-256.
5. Dispatch `.github/workflows/engineering-attestation.yml` with the Windows-package run ID, release tag, projection source commit, and projection digest.
6. Approve the `engineering-attestation-production` environment.
7. Review the App-created evidence PR after the protected scope and strict-verifier checks report success.

The resolver only qualifies run metadata. The protected job independently repeats the Product CI and Windows-package qualification, downloads the Actions artifact by numeric ID, checks the outer package and inner ZIP, signs the attestation, rechecks the current projection digest, and creates the evidence PR. It does not install dependencies or build product code.

Dispatch only from the current `main` commit. Both jobs compare the workflow commit and dispatch commit with the current `main` ref, so a workflow copied to an unmerged branch cannot reach the protected producer.

An unrelated review-repository commit does not block handoff when the canonical projection digest is unchanged. A changed projection digest aborts before the PR is created.

If the review default branch advances after the evidence PR opens, do not merge, rebase, or update that PR. Re-run the producer: it closes the stale App PR after inspecting the evidence commit's actual parent, repeats the current projection-digest check, and creates a replacement directly from the new branch head.

The dispatch input `legal_projection_source_commit` is operational lineage, not an additional v1 attestation member. The signed trust-critical value is the exact `legal_projection` object. The source commit becomes authoritative when the accepted evidence and later submission manifest bind it.

## Verify accepted evidence

After the evidence PR is accepted, read the accepted Git blobs and compare all of these values:

- the SHA-256 of the Chrome ZIP downloaded from the qualified Actions artifact;
- `artifact.sha256` in the signed attestation;
- the SHA-256 of the immutable archive `artifact.bin`;
- the SHA-256 of the `current/artifact.bin` alias.

All four values must match. The archive directory is identified by the SHA-256 of the exact final `attestation.json` bytes, including its signature and final line feed. `current/` is an intake alias and is never historical proof.

Before a later Chrome Web Store upload, use the byte-for-byte archived ZIP without repacking. The versioned submission manifest must bind at least:

- `engineering_attestation_id`;
- `engineering_attestation_sha256`;
- `engineering_attestation_signer_id`;
- `engineering_attestation_public_key_fingerprint`;
- `engineering_artifact_sha256`;
- `product_commit`;
- `store_zip_sha256`;
- `legal_projection_source_commit`;
- `legal_evidence_commit`;
- `legal_repo_commit`;
- `cws_submission_manifest_version`.

Require `store_zip_sha256 == engineering_artifact_sha256`. Resolve historical proof from the immutable archive and accepting commits, never from whatever `current/` contains later.
