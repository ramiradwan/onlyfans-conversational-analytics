# Authenticated companion session spike

This isolated research harness evaluates [ADR 0024](../../docs/adr/0024-authenticated-companion-sessions.md). It uses Noise KK from `noiseprotocol`, with synthetic pre-provisioned X25519 pins, over ordinary loopback WebSocket. It imports no production application code and changes no trust store, DNS configuration, extension permission, or production dependency.

## Run

Port 17871 must be free. The tests bind it temporarily, including in a separate hostile process; they never stop an existing listener.

From the repository root, create a separate Python 3.11+ environment outside the repository:

```powershell
python -m venv ../scratchpad/noise-review-venv
../scratchpad/noise-review-venv/Scripts/python -m pip install -r tools/companion-session-spike/requirements.txt
../scratchpad/noise-review-venv/Scripts/python tools/companion-session-spike/test_session.py
```

The suite covers encrypted bidirectional records, refusal before session confirmation, peer-pin and prologue mismatch, replay, reordering, tampering, reflection, cross-session reuse, frame limits, deadlines, cancellation, and a hostile port owner. It prints test names and payload-free errors only. All keys and payloads are synthetic and ephemeral.

## Limits

The profile is fixed to `Noise_KK_25519_ChaChaPoly_SHA256`; no protocol negotiation or plaintext fallback exists. Application data is admitted only after empty-payload handshakes and encrypted transport confirmations. Production authorization and account/consent commit fences would still be required after successful cryptographic authentication.

Pins are supplied directly by the harness. This deliberately does not implement or claim an authenticated provisioning path. The current TPM signing key is not the X25519 key used here. Review the delegation and provisioning contract before implementing production pairing.

This is Python-to-Python evidence, not a browser implementation, protocol audit, full grant-validation test, release acceptance, or proof of metadata confidentiality. WebSocket headers, lengths, timing, and connection attempts remain visible. The application record discriminator adds framing only; all encryption and authentication come from Noise.

The prototype handles one request per connection. It does not implement production storage-key operations, automatic retries, multiplexing, key rotation, session resumption, or Bridge browser security.
