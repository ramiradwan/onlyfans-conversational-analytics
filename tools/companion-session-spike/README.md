# Authenticated companion session spike

This isolated research harness evaluates [ADR 0024](../../docs/adr/0024-authenticated-companion-sessions.md). It uses Noise KK from `noiseprotocol` over ordinary loopback WebSocket, and holds the Python reference model of the [local pairing contract](../../docs/companion-pairing-contract.md). It imports no production application code and changes no trust store, DNS configuration, extension permission, or production dependency.

## Run

Port 17871 must be free. The tests bind it temporarily, including in a separate hostile process; they never stop an existing listener.

From the repository root, create a separate Python 3.11+ environment outside the repository:

```powershell
python -m venv ../scratchpad/noise-review-venv
../scratchpad/noise-review-venv/Scripts/python -m pip install -r tools/companion-session-spike/requirements.txt
../scratchpad/noise-review-venv/Scripts/python -m unittest discover -s tools/companion-session-spike -p "test_*.py" -v
```

The suite covers encrypted bidirectional records, refusal before session confirmation, peer-pin and prologue mismatch, replay, reordering, tampering, reflection, cross-session reuse, frame limits, deadlines, cancellation, and a hostile port owner. Pairing tests cover the closed message schemas, grant verification against a synthetic trust set, installation-key and account substitution, generation high-water, small-order and reflected keys, reflected nonces, role-separated low-S proofs, the Brain pairing window, and session authorization. All keys and payloads are synthetic.

The published vector is generated and checked by the reference model:

```powershell
../scratchpad/noise-review-venv/Scripts/python tools/companion-session-spike/local_pairing.py --check
../scratchpad/noise-review-venv/Scripts/python tools/companion-session-spike/local_pairing.py --write
```

`--check` fails when `extension/test-fixtures/pairing/local-pairing-vector.json` differs from a fresh build. The vector's keys derive from fixed labels and its signatures use RFC 6979 deterministic nonces, so the build is reproducible.

## Limits

The Noise profile is fixed to `Noise_KK_25519_ChaChaPoly_SHA256`; no protocol negotiation or plaintext fallback exists. Application data is admitted only after empty-payload handshakes and encrypted transport confirmations. Production authorization and account/consent commit fences remain mandatory after successful cryptographic authentication.

`local_pairing.py` implements the transcript, proofs, comparison code, session prologue, message parser, Agent offer checks, Brain request and confirmation checks, an in-memory pairing window, and session authorization. `session.py` binds each Noise session to a pairing digest through the prologue. The model signs Brain proofs with a software key; production signs through the installation-key abstraction. It does not implement durable pins, hosted revocation state, or Bridge confirmation.

This is Python-to-Python evidence, not a browser implementation, Noise library audit, production JWS library endorsement, full grant-validation test, release acceptance, or proof of metadata confidentiality. WebSocket headers, lengths, timing, and connection attempts remain visible. The application record discriminator adds framing only; all encryption and authentication come from Noise.

The prototype handles one request per connection. It does not implement production storage-key operations, automatic retries, multiplexing, Noise key rotation, session resumption, or Bridge browser security.
