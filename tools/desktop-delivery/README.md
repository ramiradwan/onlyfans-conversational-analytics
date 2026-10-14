# Direct desktop delivery

This directory owns only the HTTP download adapter. Windows App Installer owns direct MSIX replacement. Brain owns startup, migration and recovery. The existing signing-parameter Worker, keys and bucket are independent.

## Provisioned inputs

The exact nonsecret inputs are in `packaging/msix/direct-delivery.json`; the complete initial package profile is `packaging/msix/profiles/production-direct.json`. The incomplete `.template.json` remains a negative fixture, not the production profile.

- Azure signing account/profile: `Dipsy` / `simple-legal-doc-signer` at `https://swn.codesigning.azure.net/`.
- Verified Subject DN: `CN=Dipsy, O=Dipsy, L=London, C=GB`. Do not normalize it or substitute the Store publisher.
- Windows-derived direct package family: `Dipsy.OnlyFansConversationalAnalytics_bvmm2ppd090g2`.
- Stable descriptor: `https://updates.dipsy.fi/desktop/v1/windows/x64/stable/ConversationAnalytics.appinstaller`.
- Download Worker: `dipsy-desktop-downloads`; route `updates.dipsy.fi/desktop/v1/*`.
- Production R2 bucket: `dipsy-desktop-releases-prod`, EU jurisdiction.
- Isolated test-object bucket: `dipsy-desktop-qualification`, EU jurisdiction.

Initial provisioning leaves the production bucket empty. The stable descriptor returns an uncached 404 until the first qualified signed release is promoted. A reachable route is not a published release or an installed-update result. Do not upload a placeholder descriptor or an unqualified package to make the URL return 200.

Cloudflare path routes take precedence over a Worker Custom Domain on the same hostname. This adapter does not replace the signer's Custom Domain or change DNS. Both buckets have r2.dev disabled and no direct bucket custom domain.

## Serving contract

The module has no runtime dependencies and accepts only GET/HEAD. It has no upload operation, signing material, credential exchange, customer metadata or redirect fallback.

The production bucket can serve only:

```
windows/x64/stable/ConversationAnalytics.appinstaller
packages/<final-msix-sha256>/ConversationAnalytics_<four-part-version>_x64.msix
```

Paths under `qualification/<32-lowercase-hex>/` are read exclusively from the separate qualification bucket; the fixed filenames are `bytes.bin`, `probe.msix`, and `probe.appinstaller`. Missing objects return uncached 404. Unknown paths, query strings, encoded paths and non-read methods are refused.

MSIX responses use `application/msix`; descriptors use `application/appinstaller`. Downloads stream from R2, support bounded single byte ranges and If-Range/ETag checks, and disable response transformation. HEAD describes the full object without a body. Descriptor and qualification responses use no-store. Hash-addressed packages use immutable caching. A conditional R2 read prevents a concurrent descriptor update from mixing one object's headers with another's bytes.

Workers' R2 bindings provide storage access to the deployed module; 'download-only' describes the audited handler, not a separate read-only R2-binding capability. Worker deployment is an infrastructure privilege distinct from routine artifact publication.

## Test and deploy

```
cd tools/desktop-delivery
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

The pinned Miniflare runtime executes the actual Worker with R2, including range handling, object-change races, cache policy and method/path/bucket isolation. This is not an App Installer or customer journey test.

`wrangler.jsonc` declares the fixed deployment. Operator deployment requires the account's approved Workers/R2/route administration authority. Keep that authority out of the installed app and release-upload job. Disable workers.dev, previews and Worker application logging. No Worker redeployment is needed for an ordinary package release.

Before deployment, retain the old route and Worker settings, run the tests, and ensure the signer route/configuration remains unchanged. After deployment, verify live TLS, exact GET/HEAD lengths, raw bytes with compression requested, resumed ranges, MIME types, conditionals, uncached missing objects and the signer publisher-keyset route. Temporary probes belong only in the qualification bucket and are removed after the check.

## Signing and publication boundary

On 14 October 2026, a nonshipping minimal MSIX was signed using the existing Azure PublicTrust profile, timestamped, and successfully verified with Windows SignTool and Authenticode. Its exact certificate subject matched the production profile. No private signing key was exported and no production Product package was signed or published by this provisioning work.

The existing `release` GitHub environment holds bucket-scoped `DESKTOP_R2_ACCESS_KEY_ID` and `DESKTOP_R2_SECRET_ACCESS_KEY`. It also records the nonsecret variables `MSIX_DIRECT_PUBLISHER`, `MSIX_DIRECT_EXPECTED_FAMILY`, `MSIX_DIRECT_APPINSTALLER_URI`, `DESKTOP_R2_BUCKET`, `DESKTOP_R2_ENDPOINT`, and `DESKTOP_UPDATES_BASE_URL`. Freeze the resolved values with each release and require agreement with the approved profile; environment variables are not an unreviewed runtime override.

Use the EU S3 endpoint recorded in the inputs, not the non-jurisdictional endpoint. The uploader is scoped to the production desktop bucket and cannot access the signing-parameter or qualification buckets. It has no Worker/DNS/Legal administration. Its current provisioning token expires on 14 October 2027; rotate it before expiry without changing the public origin, package family or signing subject. Credentials are stored only as encrypted GitHub secrets, not in this repository or the app.

The MSIX release owner still needs to integrate the producer, signed-artifact verifier and upload/promotion steps into the protected release workflow. Build/pack before acquiring signing credentials; sign the exact output with a timestamp; run `packaging/verify-msix.ps1` with the expected family and exact subject; compute the final hash after signing. Upload immutable bytes before the descriptor. Never overwrite a hash-addressed object with different bytes. Promote the descriptor only after verifying the served package and retaining the release approval and compatibility evidence. Preserve rollback/corrective-release rules; do not enable unrestricted downgrade.

This task does not change the tag restrictions or approval mechanisms of the release environment. It does not authorize Store submission, direct release, Inno retirement, or a channel transition. Original-key, standard-user, prerequisite, graceful servicing and installed A-to-B gates remain owned by the existing MSIX implementation.

## References

- Microsoft package signing: https://learn.microsoft.com/en-us/windows/msix/package/sign-msix-package-guide
- Azure signing integration: https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations
- App Installer MIME types: https://learn.microsoft.com/en-us/windows/msix/app-installer/web-install-azure
- Cloudflare routing: https://developers.cloudflare.com/workers/configuration/routing/routes/
- R2 bindings: https://developers.cloudflare.com/r2/api/workers/workers-api-reference/
- R2 bucket-scoped credentials: https://developers.cloudflare.com/r2/api/tokens/
