# ADR 0048: Continue setup from saved installation records

- Status: Accepted.
- Date: 2026-10-09
- Decision authority: Product owner.
- Extends: ADR 0047 with a separate enrolled-installation continuation.
- Preserves: ADR 0008's hosted provisioning and local authentication boundaries.
- Governing cross-plane decisions: `creator-platform-contracts` ADR 0026,
  Continue setup for an enrolled installation, and ADR 0028, Authorize onboarding
  with current customer sessions, accepted 2026-10-09.

## Decision

A fresh native entry may resume an enrolled installation after its original setup
context has expired or been removed. The new context uses the existing activated
installation key, one exact consumed claim, and a coherent saved creator candidate.
The old journey, sessions, proofs and deadlines remain expired. This continuation
never creates a key, repeats enrollment, or uses initial admission or bootstrap
recovery to obtain creator approval. Separate installation and membership grant
acquisition and renewal retain their existing protocols and checks.

Select a finalized local account binding first, including a revoked binding; it
cannot be reassigned through recovery. Otherwise honor the current admitted exact
target. Untargeted entry selects its unique pending candidate, or its sole coherent
approved candidate when none is pending. Other approved accounts do not invalidate
a unique pending target. Conflicting histories for the same creator, unresolved
enrollment, mismatched provenance and ambiguous unnamed targets refuse automatic
selection. Row age and insertion order never select a target.

Selection is local intent, not approval. Read the key, claim, candidate and binding
in one database snapshot and recheck them in the transaction that fixes the new
context. Browser choice references are opaque and bound to current native authority,
the exact saved target and account generation. No browser body supplies arbitrary
signing coordinates. Once preparation may have committed, keep the original scope
and operation and use a non-creating result read; do not select another target or
repeat preparation to discover the result.

Use the separately published `installation-setup-continuation:v2` contract and its
isolated registered-key proof purpose. Immutable contract publication and consumer
pinning precede dependent transport implementation. The hosted service authenticates
a current owner/admin for the exact intended creator action. Existing valid approval
skips Connect. Missing approval needs one explicit Connect and its exact action
receipt under the current customer session. Receipt expiry requires resolving or
confirming that action; it does not require another provider sign-in. The closed
`authentication-required` state means an actually absent, expired or revoked session.
Neither a network error nor an unsigned HTTP status establishes that state.
A hosted projection only wakes reconciliation; the existing signed
creator binding and local verification remain authoritative. Local membership,
passkey, pairing, consent and commercial activation retain their separate checks.

Each local continuation pins its exact operation profile. Initial installation
uses the separately published handoff v2 and its exact v2 proof routes/domain.
An older prepared request is retained and refused by the v2 owner, never relabeled.
Published v1 verification fixtures remain unchanged. The new initial browser
workspace is `/public/onboarding/setup`; registered continuation retains the exact
`/public/onboarding/installation-continuation` workspace. Both use the configured
browser origin independently of the hosted API origin, keep the current browser tab,
and forward only the bounded nonauthorizing reference in an exact form body.

The separately confirmed hosted activation may return its existing proof-bound
opaque redemption continuation through a top-level form POST to the fixed local
`/provisioning/activation` entry. The body contains only the current journey and
the continuation. The receiving origin and body are checked, then the continuation
is held only in process memory for at most five minutes and the original journey's
lifetime. It never appears in a URL, browser storage, page markup or response JSON.
The entry redirects to the existing local workspace; it grants no local session.
Provisioning requires its existing session and CSRF. Runtime requires the current
creator's independent local session and CSRF. Both require the same approved
journey, installation and key, and dispatch through the existing redemption and
signed-license verification service. Activation remains an explicit hosted action.

The browser records only a nonauthorizing entry UUID before dispatch. A changed
entry, expired context, lost reply or restored page cannot automatically replay
the action. The server consumes the ephemeral secret before starting its owned
delivery task, and repeated requests read its receipt state. Signed local readiness
remains the only success authority. A process restart loses the ephemeral code and
returns an unconfirmed result; it never reconstructs or re-confirms activation.
Initial completion expiry recovery similarly retains only its original operation
profile alongside existing nonauthorizing receipt coordinates, so a v1 operation
cannot be relabeled as v2 during recovery.

No readable creator name exists in current saved records. A uniquely resolved saved
target may say “Account name unavailable” on the required Connect confirmation and
identify the stored computer. This message is unnecessary when approval already
exists. Do not derive a name from an identifier, another account, chat participants
or a new platform request. Meaningful selection between indistinguishable accounts
remains unavailable until a qualified label source exists.

Keep one worker for the exact bounded context and publish local changes after commit.
Use pushed hosted events and one-shot expiry wakeups. Unknown transport outcomes are
uncertain, without guessed causes or authority. Explicit recovery retains completed
work, and an unavailable result never means the remote mutation did not commit.

An installed extension may return the fresh context to the exact admitted setup
document under the existing document and scope fences. Recovery does not require
the extension. Without it, browser APIs cannot reliably find an arbitrary old
hosted tab: leave expired documents inactive and admit only the fresh context.
Background recovery never steals focus, navigates a user-owned unrelated document,
or reloads an OnlyFans tab.

## Verification

Verify selection and stale-selection refusal with real encrypted persistence,
including deleted old journeys, conflicting histories, preserved revoked bindings,
changed keys and unknown operations. Qualify expired and deleted provider contexts,
current authority, concurrency, rollback, stream expiry and revocation independently.
Packaged browser and installed tests must preserve the actual installation key and
prove authenticated return through local passkey creation and pairing without repeated
approval, a second first-run authentication ceremony or platform navigation.
