import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { GrantTrustError, loadGrantTrustSet } from "../transport/grant-verifier.mjs";
import {
  PairingFailure,
  SMALL_ORDER_POINTS,
  TRANSCRIPT_FIELDS,
  comparisonCode,
  encodeMessage,
  isSmallOrder,
  key32,
  lp,
  pairingTranscript,
  parseMessage,
  proofMessage,
  sessionPrologue,
  thumbprint,
  toHex,
  transcriptOf,
  unb64u,
  verifyOffer,
  verifyProof,
  verifySessionAuthorization,
} from "../transport/pairing-contract.mjs";
import { signPairingProof } from "../runtime/companion-agent-identity.mjs";

const vector = JSON.parse(
  await readFile(
    new URL("../test-fixtures/pairing/local-pairing-vector.json", import.meta.url),
    "utf8",
  ),
);
const trust = await loadGrantTrustSet(vector.trust_set, { allowNonProduction: true });
const expected = vector.expected;
const encoder = new TextEncoder();
const digest = unb64u(Buffer.from(expected.pairing_digest, "hex").toString("base64url"));

function frameOf(c) {
  const bytes = encoder.encode(c.text);
  if (c.pad_to_bytes === undefined) return bytes;
  const padded = new Uint8Array(c.pad_to_bytes).fill(0x20);
  padded.set(bytes);
  return padded;
}
const offerCheck = (c) =>
  verifyOffer(vector.request, frameOf(c), {
    trust,
    detectedAccountId: vector.detected_account_id,
    highWater: c.high_water ?? {},
    now: vector.now,
  });
const refusal = (code, detail = null) => (error) =>
  error instanceof PairingFailure && error.code === code && error.detail === detail;

test("the vector trust set is refused by the production loader", async () => {
  await assert.rejects(loadGrantTrustSet(vector.trust_set), GrantTrustError);
});

test("the positive vector verifies and reproduces every canonical value", async () => {
  const verified = await offerCheck({ text: encodeMessage(vector.offer) });
  assert.deepEqual({ ...verified.identity }, expected.identity);
  assert.equal(toHex(verified.grantDigest), expected.grant_digest);
  assert.equal(toHex(verified.pairingDigest), expected.pairing_digest);
  assert.equal(verified.comparisonCode, expected.comparison_code);
  assert.equal(verified.grantsNotAfter, expected.grants_not_after);
  assert.equal(toHex(sessionPrologue(verified.pairingDigest)), expected.session_prologue);
  assert.equal(toHex(proofMessage("brain", digest)), expected.brain_proof_message);
  assert.equal(toHex(proofMessage("agent", digest)), expected.agent_proof_message);
  assert.equal(await thumbprint(vector.request.agent_identity_jwk), expected.agent_identity_key_jkt);
  const transcript = await transcriptOf(vector.request, vector.offer, expected.identity, verified.grantDigest);
  assert.equal(toHex(pairingTranscript(transcript)), expected.transcript);
  assert.equal(
    await verifyProof(vector.request.agent_identity_jwk, "agent", digest, vector.confirm.agent_proof),
    true,
  );
  const stale = vector.offer_cases.find((c) => c.name === "stale-generation");
  assert.equal(encodeMessage(vector.offer), stale.text);
  for (const [message, type] of [
    [vector.request, "pair.request"],
    [vector.confirm, "pair.confirm"],
    [vector.result, "pair.result"],
    [vector.session_authorization, "session.authorization"],
  ])
    assert.equal(encodeMessage(parseMessage(encodeMessage(message), type)), encodeMessage(message));
});

test("every negative offer case is refused with its code and detail", async () => {
  assert.equal(vector.offer_cases.length, 29);
  for (const c of vector.offer_cases)
    await assert.rejects(offerCheck(c), refusal(c.error, c.detail), c.name);
});

test("request and confirm cases agree with the Agent parser and proof check", async () => {
  for (const c of vector.request_cases) {
    if (c.error === "pairing_message_invalid")
      assert.throws(() => parseMessage(c.text, "pair.request"), refusal(c.error), c.name);
    else parseMessage(c.text, "pair.request");
  }
  for (const c of vector.confirm_cases) {
    if (c.error === "pairing_message_invalid") {
      assert.throws(() => parseMessage(c.text, "pair.confirm"), refusal(c.error), c.name);
      continue;
    }
    const confirm = parseMessage(c.text, "pair.confirm");
    if (c.error === "pairing_proof_refused")
      assert.equal(
        await verifyProof(vector.request.agent_identity_jwk, "agent", digest, confirm.agent_proof),
        false,
        c.name,
      );
    else assert.notEqual(confirm.pairing_id, vector.offer.pairing_id, c.name);
  }
});

test("session authorization cases", async () => {
  for (const c of vector.authorization_cases) {
    const run = () => verifySessionAuthorization(c.text, expected.identity, { trust, now: c.now });
    if (c.error) await assert.rejects(run(), refusal(c.error, c.detail), c.name);
    else assert.equal(await run(), c.not_after, c.name);
  }
});

test("every transcript field changes the digest", async () => {
  const base = await transcriptOf(
    vector.request,
    vector.offer,
    expected.identity,
    Buffer.from(expected.grant_digest, "hex"),
  );
  const hash = async (t) => toHex(new Uint8Array(await crypto.subtle.digest("SHA-256", pairingTranscript(t))));
  assert.equal(await hash(base), expected.pairing_digest);
  for (const field of TRANSCRIPT_FIELDS) {
    const value = base[field];
    const changed =
      typeof value === "number"
        ? value + 1
        : typeof value === "string"
          ? value + "x"
          : Uint8Array.from(value, (x, i) => (i === 0 ? x ^ 1 : x));
    assert.notEqual(await hash({ ...base, [field]: changed }), expected.pairing_digest, field);
  }
});

test("small-order points match the reference, with and without the top bit", () => {
  assert.deepEqual(SMALL_ORDER_POINTS.map(toHex), vector.small_order_points);
  for (const point of SMALL_ORDER_POINTS) {
    const high = point.slice();
    high[31] |= 0x80;
    assert.equal(isSmallOrder(point), true);
    assert.equal(isSmallOrder(high), true);
  }
  for (let i = 0; i < 64; i += 1)
    assert.equal(isSmallOrder(crypto.getRandomValues(new Uint8Array(32))), false);
});

test("canonical inputs are bounded", async () => {
  assert.throws(() => proofMessage("bridge", digest), PairingFailure);
  assert.throws(() => proofMessage("brain", digest.slice(1)), PairingFailure);
  assert.throws(() => sessionPrologue(new Uint8Array(33)), PairingFailure);
  await assert.rejects(comparisonCode(new Uint8Array(31)), PairingFailure);
  assert.throws(() => lp("X", [-1]), PairingFailure);
  assert.throws(() => lp("X", [2 ** 53]), PairingFailure);
  assert.throws(() => key32(vector.offer.brain_noise_key + "A"), PairingFailure);
  assert.throws(() => parseMessage(encodeMessage(vector.confirm), "pair.unknown"), refusal("pairing_message_invalid"));
});

test("pairing frames use the grant parser's JSON grammar", () => {
  const text = encodeMessage(vector.confirm);
  for (const input of [
    "﻿" + text,
    text + " ",
    text + " x",
    text.replace('"pair.confirm"', '"pair.\\ud800"'),
    text.slice(0, -1) + ',"pairing_id":' + JSON.stringify(vector.confirm.pairing_id) + "}",
    text.slice(0, -1) + ',"extra":1.5}',
  ])
    assert.throws(() => parseMessage(input, "pair.confirm"), refusal("pairing_message_invalid"), input);
  assert.throws(() => parseMessage(Uint8Array.of(0xff), "pair.confirm"), refusal("pairing_message_invalid"));
  parseMessage(` \n${text}\t\r`, "pair.confirm");
});

test("the non-exportable Agent identity signs low-S pairing proofs", async () => {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, [
    "sign",
    "verify",
  ]);
  const exported = await crypto.subtle.exportKey("jwk", pair.publicKey);
  const jwk = { crv: exported.crv, kty: exported.kty, x: exported.x, y: exported.y };
  await assert.rejects(crypto.subtle.exportKey("jwk", pair.privateKey));
  for (let i = 0; i < 16; i += 1) {
    const proof = await signPairingProof({ privateKey: pair.privateKey }, digest);
    assert.equal(await verifyProof(jwk, "agent", digest, proof), true);
    assert.equal(await verifyProof(jwk, "brain", digest, proof), false);
  }
  const extractable = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
  ]);
  await assert.rejects(
    signPairingProof({ privateKey: extractable.privateKey }, digest),
    refusal("pairing_proof_refused"),
  );
});
