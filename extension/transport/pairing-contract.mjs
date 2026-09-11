// Local Agent-to-Brain pairing per docs/companion-pairing-contract.md: the
// transcript, proofs, comparison code, session prologue, message schemas, and
// the Agent checks. tools/companion-session-spike/local_pairing.py is the
// reference; both reproduce the vendored contracts/companion-pairing-v1 vectors.
import {
  GRANT_PROFILES,
  parseStrictJson,
  peekGrantClaims,
  verifyGrant,
} from "./grant-verifier.mjs";

export const SUITE = "Noise_KK_25519_ChaChaPoly_SHA256";
export const SESSION_PROFILE =
  "ofca-companion-session/v1;agent-to-brain;no-early-data";
export const PAIRING_PATH = "/ws/agent/pairing";
export const MAX_PAIRING_FRAME = 36_864;
export const MAX_GRANT_LENGTH = 16_384;
export const NOISE_TAG_BYTES = 16;
export const RECORD_DISCRIMINATOR_BYTES = 1;
const RECORD_OVERHEAD = NOISE_TAG_BYTES + RECORD_DISCRIMINATOR_BYTES;
export const MAX_APPLICATION_FRAME = 4_096;
export const MAX_APPLICATION_RECORD_PLAINTEXT =
  MAX_APPLICATION_FRAME - RECORD_OVERHEAD;
export const MAX_AUTHORIZATION_RECORD_PLAINTEXT =
  MAX_PAIRING_FRAME - RECORD_OVERHEAD;
export const MESSAGE_LIMITS = Object.freeze({
  "pair.request": MAX_PAIRING_FRAME,
  "pair.offer": MAX_PAIRING_FRAME,
  "pair.confirm": MAX_PAIRING_FRAME,
  "pair.result": MAX_PAIRING_FRAME,
  "session.authorization": MAX_AUTHORIZATION_RECORD_PLAINTEXT,
});
export const PAIRING_WINDOW_SECONDS = 300;
export const PAIRING_STEP_SECONDS = 10;
export const GRANT_AUDIENCES = Object.freeze({
  installation_grant: "urn:bridge-clean:local-brain:installation",
  creator_account_binding: "urn:bridge-clean:local-brain:creator-binding",
});
export const OUTCOMES = Object.freeze([
  "confirmed",
  "declined",
  "expired",
  "cancelled",
]);
const DOMAINS = Object.freeze({
  grants: "OFCA-LOCAL-PAIRING-GRANTS-V1",
  transcript: "OFCA-LOCAL-PAIRING-TRANSCRIPT-V1",
  proof: "OFCA-LOCAL-PAIRING-PROOF-V1",
  code: "OFCA-LOCAL-PAIRING-CODE-V1",
});
const SCHEMAS = Object.freeze({
  "pair.request": {
    agent_installation_id: "id",
    agent_identity_jwk: "jwk",
    agent_noise_key: "key",
    agent_nonce: "key",
  },
  "pair.offer": {
    pairing_id: "key",
    generation: "generation",
    creator_account_id: "id",
    brain_noise_key: "key",
    brain_nonce: "key",
    installation_jwk: "jwk",
    installation_grant: "grant",
    creator_account_binding: "grant",
    brain_proof: "signature",
  },
  "pair.confirm": { pairing_id: "key", agent_proof: "signature" },
  "pair.result": { pairing_id: "key", outcome: "outcome" },
  "session.authorization": {
    installation_grant: "grant",
    creator_account_binding: "grant",
  },
});
export const TRANSCRIPT_FIELDS = Object.freeze([
  "pairing_id",
  "generation",
  "organization_id",
  "installation_id",
  "installation_key_id",
  "installation_key_jkt",
  "creator_account_id",
  "agent_installation_id",
  "agent_identity_key_jkt",
  "agent_noise_key",
  "brain_noise_key",
  "agent_nonce",
  "brain_nonce",
  "grant_digest",
]);

export class PairingFailure extends Error {
  constructor(code = "pairing_refused", detail = null) {
    super(detail === null ? code : `${code} ${detail}`);
    this.name = "PairingFailure";
    this.code = code;
    this.detail = detail;
  }
}
export const requirePairing = (condition, code) => {
  if (!condition) throw new PairingFailure(code);
};

const enc = new TextEncoder();
const ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;
const JWS = /^[A-Za-z0-9_.-]+$/u;
const ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);
const P = BigInt(
  "0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff",
);
const B = BigInt(
  "0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b",
);

export const toHex = (bytes) =>
  [...bytes].map((x) => x.toString(16).padStart(2, "0")).join("");
export const fromHex = (value) => {
  requirePairing(typeof value === "string" && /^(?:[0-9a-f]{2})*$/u.test(value));
  return Uint8Array.from(value.match(/../gu) ?? [], (x) => parseInt(x, 16));
};
const toBigInt = (bytes) => BigInt("0x" + (toHex(bytes) || "0"));
export const sameBytes = (a, b) =>
  a instanceof Uint8Array &&
  b instanceof Uint8Array &&
  a.length === b.length &&
  a.every((x, i) => x === b[i]);

export function b64u(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "");
}
export function unb64u(value) {
  requirePairing(typeof value === "string" && /^[A-Za-z0-9_-]+$/u.test(value));
  let result;
  try {
    result = Uint8Array.from(
      atob(
        value.replace(/-/gu, "+").replace(/_/gu, "/") +
          "=".repeat((4 - (value.length % 4)) % 4),
      ),
      (ch) => ch.charCodeAt(0),
    );
  } catch {
    throw new PairingFailure();
  }
  requirePairing(b64u(result) === value);
  return result;
}
export function key32(value) {
  requirePairing(typeof value === "string" && value.length === 43);
  const bytes = unb64u(value);
  requirePairing(bytes.length === 32);
  return bytes;
}
export function exact(object, fields) {
  requirePairing(
    object !== null &&
      typeof object === "object" &&
      !Array.isArray(object) &&
      Object.keys(object).length === fields.length &&
      fields.every((k) => Object.hasOwn(object, k)),
  );
}

export function lp(domain, fields) {
  const parts = [enc.encode(domain + "\0")];
  for (const field of fields) {
    let bytes;
    if (typeof field === "string") bytes = enc.encode(field);
    else if (typeof field === "number") {
      requirePairing(Number.isSafeInteger(field) && field >= 0);
      bytes = new Uint8Array(8);
      new DataView(bytes.buffer).setBigUint64(0, BigInt(field));
    } else {
      requirePairing(field instanceof Uint8Array);
      bytes = field;
    }
    const length = new Uint8Array(4);
    new DataView(length.buffer).setUint32(0, bytes.length);
    parts.push(length, bytes);
  }
  const result = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    result.set(p, offset);
    offset += p.length;
  }
  return result;
}
export const digest = async (bytes) =>
  new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));

// libsodium's X25519 small-order encodings, compared with the top bit cleared.
export const SMALL_ORDER_POINTS = Object.freeze(
  [
    "00".repeat(32),
    "01" + "00".repeat(31),
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "ec" + "ff".repeat(30) + "7f",
    "ed" + "ff".repeat(30) + "7f",
    "ee" + "ff".repeat(30) + "7f",
  ].map(fromHex),
);
export function isSmallOrder(key) {
  requirePairing(key instanceof Uint8Array && key.length === 32);
  const masked = key.slice();
  masked[31] &= 0x7f;
  return SMALL_ORDER_POINTS.some((point) => sameBytes(point, masked));
}

export function lowSignature(signature) {
  requirePairing(signature instanceof Uint8Array && signature.length === 64);
  const r = toBigInt(signature.slice(0, 32)),
    s = toBigInt(signature.slice(32));
  requirePairing(r > 0n && r < ORDER && s > 0n && s <= ORDER / 2n);
}
export function normalizeSignature(signature) {
  requirePairing(signature instanceof Uint8Array && signature.length === 64);
  const s = toBigInt(signature.slice(32));
  if (s > ORDER / 2n)
    signature.set(fromHex((ORDER - s).toString(16).padStart(64, "0")), 32);
  lowSignature(signature);
  return signature;
}

/** Exactly {crv, kty, x, y} on P-256, and a point on the curve. */
export function publicJwk(jwk) {
  exact(jwk, ["crv", "kty", "x", "y"]);
  requirePairing(jwk.crv === "P-256" && jwk.kty === "EC");
  const x = toBigInt(key32(jwk.x)),
    y = toBigInt(key32(jwk.y));
  requirePairing(
    x < P && y < P && (y * y) % P === (((x * x - 3n) * x + B) % P + P) % P,
  );
  return { crv: "P-256", kty: "EC", x: jwk.x, y: jwk.y };
}
/** RFC 7638 SHA-256 thumbprint as unpadded base64url. */
export async function thumbprint(jwk) {
  const bare = publicJwk({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y });
  return b64u(await digest(enc.encode(JSON.stringify(bare))));
}

export const grantDigest = async (creatorAccountBinding, installationGrant) =>
  digest(
    lp(DOMAINS.grants, [
      "creator_account_binding",
      await digest(enc.encode(creatorAccountBinding)),
      "installation_grant",
      await digest(enc.encode(installationGrant)),
    ]),
  );
export const pairingTranscript = (t) =>
  lp(DOMAINS.transcript, [SUITE, ...TRANSCRIPT_FIELDS.map((name) => t[name])]);
export const pairingDigest = (t) => digest(pairingTranscript(t));
export function proofMessage(role, pairingDigestBytes) {
  requirePairing(
    (role === "agent" || role === "brain") &&
      pairingDigestBytes instanceof Uint8Array &&
      pairingDigestBytes.length === 32,
  );
  return lp(DOMAINS.proof, [role, pairingDigestBytes]);
}
export async function comparisonCode(pairingDigestBytes) {
  requirePairing(pairingDigestBytes instanceof Uint8Array && pairingDigestBytes.length === 32);
  const hash = await digest(lp(DOMAINS.code, [pairingDigestBytes]));
  const value = new DataView(hash.buffer).getUint32(0);
  return String(value % 1_000_000).padStart(6, "0");
}
export function sessionPrologue(pairingDigestBytes) {
  requirePairing(pairingDigestBytes instanceof Uint8Array && pairingDigestBytes.length === 32);
  const prefix = enc.encode(SESSION_PROFILE + "\0");
  const prologue = new Uint8Array(prefix.length + 32);
  prologue.set(prefix);
  prologue.set(pairingDigestBytes, prefix.length);
  return prologue;
}

export async function verifyProof(jwk, role, pairingDigestBytes, signature) {
  try {
    const raw = unb64u(signature);
    lowSignature(raw);
    const key = await crypto.subtle.importKey(
      "jwk",
      publicJwk(jwk),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      key,
      raw,
      proofMessage(role, pairingDigestBytes),
    );
  } catch {
    return false;
  }
}

function validField(kind, value) {
  if (kind === "id") return typeof value === "string" && ID.test(value);
  if (kind === "key") return key32(value).length === 32;
  if (kind === "signature")
    return typeof value === "string" && value.length === 86 && unb64u(value).length === 64;
  if (kind === "jwk") return Boolean(publicJwk(value));
  if (kind === "grant")
    return typeof value === "string" && value.length <= MAX_GRANT_LENGTH && JWS.test(value);
  if (kind === "generation") return Number.isSafeInteger(value) && value >= 1;
  return typeof value === "string" && OUTCOMES.includes(value);
}

/** Parse one pairing text frame under its closed schema. */
export function parseMessage(frame, type) {
  try {
    requirePairing(typeof frame !== "string" || frame.isWellFormed());
    const bytes = typeof frame === "string" ? enc.encode(frame) : frame;
    const limit = Object.hasOwn(MESSAGE_LIMITS, type) ? MESSAGE_LIMITS[type] : 0;
    requirePairing(bytes instanceof Uint8Array && bytes.length <= limit);
    const schema = Object.hasOwn(SCHEMAS, type) ? SCHEMAS[type] : null;
    const message = parseStrictJson(bytes);
    requirePairing(schema && message.type === type);
    exact(message, ["type", ...Object.keys(schema)]);
    for (const [name, kind] of Object.entries(schema))
      requirePairing(validField(kind, message[name]));
    return message;
  } catch {
    throw new PairingFailure("pairing_message_invalid");
  }
}
export const encodeMessage = (message) => JSON.stringify(message);

const claimText = (claims, name) =>
  claims && typeof claims[name] === "string" ? claims[name] : "";

/**
 * Verify both Brain-audience grants for one identity (organization_id,
 * installation_id, installation_key_id, installation_key_jkt,
 * creator_account_id); return the earliest exp plus grace.
 */
export async function verifyGrants(
  installationGrant,
  creatorAccountBinding,
  identity,
  { trust, now },
) {
  let notAfter = null;
  for (const [grantType, token] of [
    ["installation_grant", installationGrant],
    ["creator_account_binding", creatorAccountBinding],
  ]) {
    let subject = `installation:${identity.installation_id}`;
    if (grantType === "creator_account_binding")
      subject += `:creator:${identity.creator_account_id}`;
    const outcome = await verifyGrant(
      token,
      {
        expectedGrantType: grantType,
        expectedAudience: GRANT_AUDIENCES[grantType],
        expectedOrganizationId: identity.organization_id,
        expectedInstallationId: identity.installation_id,
        expectedInstallationKeyId: identity.installation_key_id,
        expectedInstallationKeyJkt: identity.installation_key_jkt,
        expectedSubject: subject,
        verifierTime: now,
        tombstones: [],
      },
      trust,
    );
    if (!outcome.valid)
      throw new PairingFailure("pairing_grant_refused", `${grantType}:${outcome.result}`);
    const limit = peekGrantClaims(token).exp + GRANT_PROFILES[grantType].grace;
    notAfter = notAfter === null ? limit : Math.min(notAfter, limit);
  }
  if (claimText(peekGrantClaims(creatorAccountBinding), "creator_account_id") !== identity.creator_account_id)
    throw new PairingFailure("pairing_account_refused");
  return notAfter;
}

export async function transcriptOf(request, offer, identity, grants) {
  return {
    pairing_id: key32(offer.pairing_id),
    generation: offer.generation,
    organization_id: identity.organization_id,
    installation_id: identity.installation_id,
    installation_key_id: identity.installation_key_id,
    installation_key_jkt: identity.installation_key_jkt,
    creator_account_id: identity.creator_account_id,
    agent_installation_id: request.agent_installation_id,
    agent_identity_key_jkt: await thumbprint(request.agent_identity_jwk),
    agent_noise_key: key32(request.agent_noise_key),
    brain_noise_key: key32(offer.brain_noise_key),
    agent_nonce: key32(request.agent_nonce),
    brain_nonce: key32(offer.brain_nonce),
    grant_digest: grants,
  };
}

/**
 * Agent checks for pair.offer, in contract order. highWater maps installation_id
 * to the highest admitted generation.
 */
export async function verifyOffer(request, frame, { trust, detectedAccountId, highWater, now }) {
  const offer = parseMessage(frame, "pair.offer");
  const agentNoiseKey = key32(request.agent_noise_key),
    brainNoiseKey = key32(offer.brain_noise_key);
  if (isSmallOrder(brainNoiseKey) || sameBytes(brainNoiseKey, agentNoiseKey))
    throw new PairingFailure("pairing_key_refused");
  if (offer.brain_nonce === request.agent_nonce)
    throw new PairingFailure("pairing_nonce_refused");
  const claims = peekGrantClaims(offer.installation_grant);
  const identity = Object.freeze({
    organization_id: claimText(claims, "organization_id"),
    installation_id: claimText(claims, "installation_id"),
    installation_key_id: claimText(claims, "installation_key_id"),
    installation_key_jkt: await thumbprint(offer.installation_jwk),
    creator_account_id: offer.creator_account_id,
  });
  const grantsNotAfter = await verifyGrants(
    offer.installation_grant,
    offer.creator_account_binding,
    identity,
    { trust, now },
  );
  if (offer.creator_account_id !== detectedAccountId)
    throw new PairingFailure("pairing_account_refused");
  const floor = highWater && Object.hasOwn(highWater, identity.installation_id)
    ? highWater[identity.installation_id]
    : 0;
  if (offer.generation <= floor) throw new PairingFailure("pairing_generation_refused");
  const grants = await grantDigest(offer.creator_account_binding, offer.installation_grant);
  const pairing = await pairingDigest(await transcriptOf(request, offer, identity, grants));
  if (!(await verifyProof(offer.installation_jwk, "brain", pairing, offer.brain_proof)))
    throw new PairingFailure("pairing_proof_refused");
  return Object.freeze({
    offer,
    identity,
    pairingId: offer.pairing_id,
    generation: offer.generation,
    brainNoiseKey,
    grantDigest: grants,
    pairingDigest: pairing,
    comparisonCode: await comparisonCode(pairing),
    grantsNotAfter,
  });
}

/** Agent check for the first session record; returns the session limit. */
export async function verifySessionAuthorization(frame, identity, { trust, now }) {
  const record = parseMessage(frame, "session.authorization");
  return verifyGrants(record.installation_grant, record.creator_account_binding, identity, {
    trust,
    now,
  });
}
