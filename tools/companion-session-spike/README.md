# Authenticated companion session spike

This isolated research harness evaluates [ADR 0024](../../docs/adr/0024-authenticated-companion-sessions.md). It uses Noise KK from `noiseprotocol`, with synthetic authority-signed X25519 pairing receipts, over ordinary loopback WebSocket. It imports no production application code and changes no trust store, DNS configuration, extension permission, or production dependency.

## Run

Port 17871 must be free. The tests bind it temporarily, including in a separate hostile process; they never stop an existing listener.

From the repository root, create a separate Python 3.11+ environment outside the repository:

```powershell
python -m venv ../scratchpad/noise-review-venv
../scratchpad/noise-review-venv/Scripts/python -m pip install -r tools/companion-session-spike/requirements.txt
../scratchpad/noise-review-venv/Scripts/python -m unittest discover -s tools/companion-session-spike -p "test_*.py" -v
```

The suite covers encrypted bidirectional records, refusal before session confirmation, peer-pin and prologue mismatch, replay, reordering, tampering, reflection, cross-session reuse, frame limits, deadlines, cancellation, and a hostile port owner. Bootstrap tests additionally cover a closed ES256 receipt profile, purpose-scoped issuer keys, exact installation/Agent signing identities, tenant/account binding, independent endpoint challenges, pairing lineage/generation, approval/grant context, durable replay/high-water inputs, cancellation fences, receipt-vs-offline lifetime separation, domain-separated possession-proof transcripts, and a fixed cross-runtime Noise-binding vector. All keys and payloads are synthetic and ephemeral.

## Limits

The Noise profile is fixed to `Noise_KK_25519_ChaChaPoly_SHA256`; no protocol negotiation or plaintext fallback exists. Application data is admitted only after empty-payload handshakes and encrypted transport confirmations. Production authorization and account/consent commit fences remain mandatory after successful cryptographic authentication.

`bootstrap.py` models receipt verification against an independently supplied `pairing-receipt` issuer key. It parses compact JWS before semantic use, rejects duplicate JSON members, embedded/remote key references, unknown/incorrect-purpose keys, noncanonical base64url, extra claims, high-S/non-P1363 signatures, identity/context/key substitution, generation rollback, consumed receipt IDs, and expired enrollment. The verified typed claims are encoded with a fixed domain-separated length-prefixed binary profile and SHA-256 for the Noise prologue; JWS signature bytes and `kid` are intentionally excluded.

The prototype also exposes canonical enrollment-digest and endpoint possession-proof message builders so the hosted issuer, MV3 Agent, and Brain can share exact vectors. It distinguishes five-minute receipt admission from the separately authorized `offline_not_after` pin/session lease. The in-memory gate can be seeded with highest-generation, revocation-floor, and consumed-receipt state to test production persistence rules, but it is not itself durable storage.

The authority, enrollment state, customer approval, Agent identity registration, TPM proof verification, grant validation, issuer-key distribution/rotation, hosted generation allocator, revocation delivery, and production offline policy are not implemented here. See the [pairing contract](../../docs/companion-pairing-contract.md).

This is Python-to-Python evidence, not a browser implementation, Noise library audit, production JWS library endorsement, full grant-validation test, release acceptance, or proof of metadata confidentiality. WebSocket headers, lengths, timing, and connection attempts remain visible. The application record discriminator adds framing only; all encryption and authentication come from Noise.

The prototype handles one request per connection. It does not implement production storage-key operations, automatic retries, multiplexing, Noise key rotation, session resumption, or Bridge browser security.

The gate checks the offline lease when creating a session using caller-supplied time. It does not schedule expiry of an already active session or detect clock rollback within the lease. Production must enforce the lease throughout session use and fail closed when time cannot be trusted.
