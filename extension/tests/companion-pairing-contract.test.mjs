import test from "node:test";
import assert from "node:assert/strict";
import { GrantTrustError, loadGrantTrustSet } from "../transport/grant-verifier.mjs";
import {
  MAX_APPLICATION_FRAME,
  MAX_APPLICATION_RECORD_PLAINTEXT,
  MAX_PAIRING_FRAME,
  MESSAGE_LIMITS,
  NOISE_TAG_BYTES,
  PairingFailure,
  RECORD_DISCRIMINATOR_BYTES,
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
import {
  authorizationCases,
  confirmCases,
  offerCases,
  profile,
  requestCases,
  trustSet,
  vector,
} from "../test-fixtures/pairing/vendored-vector.mjs";

const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
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

test("the vendored trust set is refused by the production loader", async () => {
  await assert.rejects(loadGrantTrustSet(trustSet), GrantTrustError);
});

test("the Agent implements the vendored profile record", () => {
  assert.equal(profile.profile, vector.profile);
  assert.equal(profile.suite, "Noise_KK_25519_ChaChaPoly_SHA256");
  assert.deepEqual(
    profile.transcript.fields.map((field) => field.name).slice(1),
    [...TRANSCRIPT_FIELDS],
  );
  assert.deepEqual(profile.small_order_noise_keys.keys, SMALL_ORDER_POINTS.map(toHex));
  assert.equal(profile.limits.max_frame_bytes, MAX_PAIRING_FRAME);
  assert.equal(profile.limits.max_application_frame_bytes, MAX_APPLICATION_FRAME);
  assert.equal(profile.limits.noise_tag_bytes, NOISE_TAG_BYTES);
  assert.equal(
    profile.limits.record_discriminator_bytes,
    RECORD_DISCRIMINATOR_BYTES,
  );
  assert.equal(
    profile.limits.max_application_record_plaintext_bytes,
    MAX_APPLICATION_RECORD_PLAINTEXT,
  );
  assert.equal(
    profile.limits.max_authorization_record_plaintext_bytes,
    MESSAGE_LIMITS["session.authorization"],
  );
  const overhead =
    profile.limits.noise_tag_bytes + profile.limits.record_discriminator_bytes;
  assert.equal(
    profile.limits.max_application_record_plaintext_bytes,
    profile.limits.max_application_frame_bytes - overhead,
  );
  assert.equal(
    profile.limits.max_authorization_record_plaintext_bytes,
    profile.limits.max_frame_bytes - overhead,
  );
  const filler = "a".repeat(profile.limits.max_grant_characters);
  assert.equal(
    Buffer.byteLength(
      JSON.stringify({
        type: "session.authorization",
        installation_grant: filler,
        creator_account_binding: filler,
      }),
      "utf8",
    ),
    profile.limits.max_authorization_envelope_bytes,
  );
  assert.ok(
    profile.limits.max_authorization_envelope_bytes <=
      profile.limits.max_authorization_record_plaintext_bytes,
    "two grants at the published limit must fit one authorization record",
  );
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
  const stale = offerCases.find((c) => c.name === "stale-generation");
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
  assert.equal(offerCases.length, 28);
  for (const c of offerCases)
    await assert.rejects(offerCheck(c), refusal(c.error, c.detail), c.name);
});

test("request and confirm cases agree with the Agent parser and proof check", async () => {
  for (const c of requestCases) {
    if (c.error === "pairing_message_invalid")
      assert.throws(() => parseMessage(c.text, "pair.request"), refusal(c.error), c.name);
    else parseMessage(c.text, "pair.request");
  }
  for (const c of confirmCases) {
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
  for (const c of authorizationCases) {
    const run = () =>
      verifySessionAuthorization(frameOf(c), expected.identity, { trust, now: c.now });
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
  assert.deepEqual(vector.small_order_points, profile.small_order_noise_keys.keys);
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
