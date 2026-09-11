import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  CONTEXT_FIELDS,
  PairingFailure,
  b64u,
  unb64u,
  raw32,
  toHex,
  enrollmentDigest,
  proofMessage,
  registrationMessage,
  grantDigest,
  receiptBinding,
  flatObject,
  loadTrustSet,
  verifyReceipt,
  thumbprint,
} from "../transport/pairing-contract.mjs";
import {
  signAgentRegistration,
  signAgentPairing,
} from "../runtime/companion-agent-identity.mjs";
const vector = JSON.parse(
  await readFile(
    new URL("../test-fixtures/pairing/authority-vector.json", import.meta.url),
    "utf8",
  ),
);
const now = vector.receipt_claims.iat;
const key = await crypto.subtle.importKey(
  "jwk",
  vector.fixture_key.public_jwk,
  { name: "ECDSA", namedCurve: "P-256" },
  false,
  ["verify"],
);
const trust = {
  sequence: 1,
  lookup: (kid) => (kid === vector.fixture_key.kid ? key : undefined),
};
const options = {
  trust,
  expected: vector.receipt_claims,
  now,
  deadline: now + 300,
};
test("hosted authority vector reproduces every canonical digest and verifies ES256", async () => {
  assert.equal(
    toHex(await enrollmentDigest(vector.receipt_claims)),
    vector.enrollment_digest_hex,
  );
  assert.equal(
    toHex(await receiptBinding(vector.receipt_claims)),
    vector.receipt_binding_hex,
  );
  assert.equal(
    toHex(
      proofMessage(
        "agent",
        raw32(vector.agent_proof.issuer_challenge_hex),
        raw32(vector.enrollment_digest_hex),
        vector.agent_proof.identity_key_id,
      ),
    ),
    vector.agent_proof.message_hex,
  );
  assert.equal(
    toHex(
      await grantDigest(
        vector.grant_digest.creator_account_binding_sha256,
        vector.grant_digest.installation_grant_sha256,
      ),
    ),
    vector.grant_digest.combined_hex,
  );
  const result = await verifyReceipt(vector.signed_jws, options);
  assert.equal(toHex(result.binding), vector.receipt_binding_hex);
  assert.equal(result.claims.iss, "https://control.creatorapp.ai");
});
test("receipt refuses every context substitution and admission boundary", async () => {
  for (const field of CONTEXT_FIELDS)
    await assert.rejects(
      verifyReceipt(vector.signed_jws, {
        ...options,
        expected: { ...vector.receipt_claims, [field]: "substituted" },
      }),
      PairingFailure,
      field,
    );
  for (const time of [now - 1, now + 300, NaN, Infinity, 1.5])
    await assert.rejects(
      verifyReceipt(vector.signed_jws, { ...options, now: time }),
      PairingFailure,
    );
  await assert.rejects(
    verifyReceipt(vector.signed_jws, { ...options, deadline: now }),
    PairingFailure,
  );
});
test("closed JSON rejects duplicate escaped keys, nesting, trailing data and malformed UTF8", () => {
  const encode = (s) => new TextEncoder().encode(s);
  for (const text of [
    '{"kid":"a","k\\u0069d":"b"}',
    '{"a":{}}',
    '{"a":[]}',
    '{"a":true}',
    '{"a":1,}',
    "{} garbage",
    '{"a":NaN}',
  ])
    assert.throws(() => flatObject(encode(text), 512), PairingFailure, text);
  assert.throws(() => flatObject(Uint8Array.of(0xff), 512), PairingFailure);
});
test("signature, encoding, header and key-source confusion fail closed", async () => {
  const [h, p, s] = vector.signed_jws.split(".");
  const changedHeader = (obj) =>
    b64u(new TextEncoder().encode(JSON.stringify(obj))) + "." + p + "." + s;
  const header = JSON.parse(vector.protected_header_utf8);
  for (const token of [
    h + "=." + p + "." + s,
    h + "." + p + "." + s + "=",
    h + "." + p + "." + b64u(new Uint8Array(64)),
    changedHeader({ ...header, alg: "none" }),
    changedHeader({ ...header, jku: "https://example.invalid" }),
    changedHeader({ ...header, kid: "unknown" }),
    vector.signed_jws + ".",
    "x".repeat(8193),
  ])
    await assert.rejects(verifyReceipt(token, options), PairingFailure);
  const sig = unb64u(s),
    order = BigInt(
      "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
    );
  sig.set(
    raw32(
      (order - BigInt("0x" + toHex(sig.slice(32))))
        .toString(16)
        .padStart(64, "0"),
    ),
    32,
  );
  await assert.rejects(
    verifyReceipt(h + "." + p + "." + b64u(sig), options),
    PairingFailure,
  );
});
test("production trust loader rejects template and fixture keys; checks exact thumbprint and purpose", async () => {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey),
    jkt = await thumbprint(jwk),
    kid = "pr1." + b64u(unb64u(jkt).slice(0, 16));
  const entry = {
    alg: "ES256",
    fixture_only: false,
    jwk: { crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y, kid },
    kid,
    purpose: "pairing-receipt",
    thumbprint: jkt,
  };
  const t = {
    environment: "production",
    issuer: options.expected.iss,
    keys: [entry],
    production_usable: true,
    profile: "pairing-receipt-v1",
    transition_sequence: 1,
  };
  assert.ok((await loadTrustSet(t)).lookup(kid));
  for (const change of [
    { production_usable: false },
    { keys: [] },
    { transition_sequence: 0 },
    { keys: [{ ...entry, fixture_only: true }] },
    { keys: [{ ...entry, purpose: "installation-binding" }] },
    { keys: [{ ...entry, thumbprint: vector.fixture_key.thumbprint }] },
    { keys: [entry, entry] },
  ])
    await assert.rejects(loadTrustSet({ ...t, ...change }), PairingFailure);
});
test("dedicated non-exportable identity signs purpose-separated registration and pairing messages", async () => {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const identity = {
    privateKey: pair.privateKey,
    thumbprint: await thumbprint(
      await crypto.subtle.exportKey("jwk", pair.publicKey),
    ),
  };
  await assert.rejects(crypto.subtle.exportKey("jwk", identity.privateKey));
  const c = {
      ...vector.receipt_claims,
      agent_identity_key_jkt: identity.thumbprint,
    },
    challenge = new Uint8Array(32).fill(7);
  const sig = await signAgentPairing(identity, c, challenge);
  assert.equal(
    await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      pair.publicKey,
      sig,
      proofMessage(
        "agent",
        challenge,
        await enrollmentDigest(c),
        c.agent_identity_key_id,
      ),
    ),
    true,
  );
  const registration = await signAgentRegistration(identity, {
    challenge,
    organizationId: c.organization_id,
    agentId: c.agent_id,
    keyId: c.agent_identity_key_id,
    thumbprint: identity.thumbprint,
  });
  assert.notDeepEqual(registration, sig);
  assert.equal(await crypto.subtle.verify(
    {name: "ECDSA", hash: "SHA-256"}, pair.publicKey, registration,
    registrationMessage(challenge, c.organization_id, c.agent_id, c.agent_identity_key_id, identity.thumbprint),
  ), true);
  assert.equal(await crypto.subtle.verify(
    {name: "ECDSA", hash: "SHA-256"}, pair.publicKey, registration,
    proofMessage("agent", challenge, await enrollmentDigest(c), c.agent_identity_key_id),
  ), false);
  await assert.rejects(
    signAgentPairing(identity, vector.receipt_claims, challenge),
    PairingFailure,
  );
  const snapshot = structuredClone(c), frozenChallenge = challenge.slice();
  const pending = signAgentPairing(identity, c, challenge);
  c.agent_identity_key_id = "substituted";
  challenge.fill(8);
  assert.equal(await crypto.subtle.verify(
    {name: "ECDSA", hash: "SHA-256"}, pair.publicKey, await pending,
    proofMessage("agent", frozenChallenge, await enrollmentDigest(snapshot), snapshot.agent_identity_key_id),
  ), true);
});

test("JSON numeric representation and whitespace match the authority parser", () => {
  for (const input of ['{"generation":1.0}', '{"generation":1e0}', '\uFEFF{}', '{}\u00A0']) {
    assert.throws(() => flatObject(new TextEncoder().encode(input), 512), PairingFailure);
  }
});
