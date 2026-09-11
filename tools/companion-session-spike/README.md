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

The suite covers encrypted bidirectional records, refusal before session confirmation, peer-pin and prologue mismatch, replay, reordering, tampering, reflection, cross-session reuse, frame limits, deadlines, cancellation, and a hostile port owner. It prints test names and payload-free errors only. All keys and payloads are synthetic and ephemeral.

## Limits

The profile is fixed to `Noise_KK_25519_ChaChaPoly_SHA256`; no protocol negotiation or plaintext fallback exists. Application data is admitted only after empty-payload handshakes and encrypted transport confirmations. Production authorization and account/consent commit fences would still be required after successful cryptographic authentication.

`bootstrap.py` verifies purpose-specific ES256 pairing receipts against an independently supplied pinned authority key. It checks local key ownership, both challenges, installation/account/Agent context, suite, generation, and validity before admitting peer pins. `test_bootstrap.py` then uses those verified pins in Noise. The authority and enrollment state are synthetic; the hosted issuer, TPM proof, production grant checks, durable transactions, and browser verifier are not implemented here. See the [pairing contract proposal](../../docs/companion-pairing-contract.md).

This is Python-to-Python evidence, not a browser implementation, protocol audit, full grant-validation test, release acceptance, or proof of metadata confidentiality. WebSocket headers, lengths, timing, and connection attempts remain visible. The application record discriminator adds framing only; all encryption and authentication come from Noise.

The prototype handles one request per connection. It does not implement production storage-key operations, automatic retries, multiplexing, key rotation, session resumption, or Bridge browser security.
