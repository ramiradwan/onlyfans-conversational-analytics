# ADR 0060: Enroll one account passkey and verify local access independently

- Status: Accepted
- Proposed amendment, 25 November 2026. Define the explicit local lock for a release using account passkeys.
- Date: 2026-11-24
- Decision authority: Rami Radwan, product and architecture owner, accepted reviewed revision 12d50d1cafbb0bd3723ad2f841da52e7b5b3e9b0 on 2026-11-24. Security, Identity and Legal recorded their design decisions on the same date.
- Supersedes: for this journey replace the enrollment order in ADR 0008, “Local Bridge authentication,” and the first-session bootstrap only. Hosted registration may precede Connect. Brain still verifies the committed local binding and applicable grants, owns every local session, and enforces the saved-data and new-work boundaries in ADRs 0008 and 0014.

## Decision

Use one native Windows Hello enrollment on each supported computer for both hosted account sign-in and independent access to saved data. Brain remains the sole issuer of local sessions. Introduce a restricted desktop operation for enrollment and local verification. The desktop broker derives the RP, caller policy and challenge from trusted configuration and the authenticated operation. Restrict the broker to the owned onboarding operation and its trusted Windows result. After the Hello prompt, use the existing non-exportable installation key to sign an exact enrollment completion proof for that result. The Provider checks the signature against the key pinned to the account's setup operation. Windows checks the package publisher locally. Keep the local broker's authority separate from the hosted proof.

In the Account step, explain the enrollment purpose and request one native creation when the app is in the foreground after an eligible sign-in. Require explicit intent and verified presence and user verification. Allow a deliberate retry and **Finish later**. Enrollment can complete before **Connect account**. A browser return prompts Brain to read the proof-bound result. The customer still decides whether to connect the specific computer and resource. Declining leaves the installation unconnected. Only an authorized, committed connection permits the signed local binding and initial local access.

Brain verifies the signed public credential binding for the expected account, installation, RP, key and generation. It enforces authenticated withdrawals and detects rollback. An independent, nonce-bound, single-use authorization can open the first local session after a new verified enrollment and committed connection. Limit it to source validity and 300 seconds from verified enrollment. Proof-bound receipt retrieval returns the same committed result. Its deadline is at most 1,800 seconds after commit and is capped by the authorized recovery deadline. Receipt recovery leaves the original first-local-access deadline and consumption state in force. After restart, expiry or consumption, use a fresh, purpose-bound local assertion with at least 32 random challenge bytes and a 120-second lifetime. Keep biometric data, PINs and private keys within Windows Hello.

Use the existing local authorization owner and distinct saved-data session. After a disconnected cold boot and expiry of hosted sessions and signed grant grace, the enrolled user can view, export and delete previously saved data with fresh local verification. Starting or resuming protected work continues to require its own current authority. A hosted outage never renews a grant. Device disconnection and passkey withdrawal apply only within their authenticated scopes. The app can act on a remote withdrawal once it receives it. Removing a local OS entry requires its own Windows action.

## Why

The same credential can answer independent browser and native challenges. The installed application already separates authenticated access to existing data from authorization for new work. One enrollment combines these capabilities while preserving the existing privacy and local authority boundaries. Durable binding records establish which public key belongs to this installation. An actual fresh proof establishes the person’s current local access.

## Consequences

- Browser access keeps ADR 0050's one-use document admission and current generation check. Sign-in and pairing use their own established boundaries. Commercial activation, license state and capture readiness retain their separate decisions. An uncompleted or cancelled enrollment leaves a resumable setup.
- For removal, show the exact selected credential and whether it is the last local means of access. Require fresh same-account verification and explicit confirmation of the effects. Offer a way to set up a replacement or recover access to the same account. Apply an authenticated withdrawal to the matching local binding. Hosted sign-out leaves the OS credential available.
- If Windows withdraws passkey permission, guide the user to restore permission and use their existing credential. Define the reported outcomes for missing Hello, other managers and phone-only credentials. A result that cannot verify a fresh local assertion leaves local data locked.
- The full supported range still needs installed evidence. Carry physical Windows 11 25H2, competing passkey managers, unavailable Hello and phone-only credentials into release qualification. Record the physical 26H2 and virtual 25H2 outcomes separately.
- Qualify same-credential hosted and disconnected use, failure after source expiry, cancellation, retries, mismatched account or installation, known local withdrawal, first pairing, restart, and precise saved-data access after grant expiry. Public Product consumes approved generated artifacts by immutable digest.

- Make support reachable outside sign-in. Present a minimized reference for Identity's human review of a refusal. Keep information about a specific account within its verified customer context. Guide customers with a valid local credential to saved-data access. Verify the support route after refusal and supply the verified behavior for Legal's final review of the sign-in and account surfaces.

## Local app lock

The configured desktop has an Account entry. Its passkey management action opens the selected release's account page in Chrome. The hosted service verifies the browser session and owns credential removal.

Lock Dipsy ends ordinary local sessions and their dependent tickets, closes protected views and pauses new capture and analysis on this installation. It retains the pairing, credential, account grants and saved records. A fresh ordinary Windows Hello verification can resume work under the current grants. An independent saved-data verification opens the archive under its existing rules. The app records an explicit lock across restart. A verification already underway when the lock is applied cannot clear it.

## Related

[Production authentication and saved data](0008-production-authentication.md) · [Local authorization](0014-local-authorization-foundation.md) · [Browser task admission](0050-desktop-onboarding-and-browser-tasks.md) · [Native onboarding decisions](0051-native-account-and-activation-decisions.md) · [Onboarding coordination](0054-consolidate-onboarding-coordination.md) · [Installed package](0056-msix-packaging-and-managed-updates.md)
