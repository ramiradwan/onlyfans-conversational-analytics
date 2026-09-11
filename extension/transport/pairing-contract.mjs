// Closed, flat receipt profile; cryptographic operations use WebCrypto.
export const ISSUER = "https://control.creatorapp.ai";
export const AUDIENCE = "urn:ofca:companion-pairing:v1";
export const SUITE = "Noise_KK_25519_ChaChaPoly_SHA256";
export const TYPE = "ofca-companion-pairing+jwt";
export class PairingFailure extends Error {
  constructor(code = "pairing_refused") {
    super(code);
    this.name = "PairingFailure";
    this.code = code;
  }
}
export const requirePairing = (condition) => {
  if (!condition) throw new PairingFailure();
};
const enc = new TextEncoder();
const id = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;
const ids = [
  "organization_id",
  "installation_id",
  "installation_key_id",
  "agent_id",
  "agent_identity_key_id",
  "account_id",
  "approval_id",
];
const hexes = [
  "jti",
  "pairing_id",
  "agent_nonce",
  "brain_nonce",
  "grant_digest",
];
const keys = [
  "installation_key_jkt",
  "agent_identity_key_jkt",
  "agent_key",
  "brain_key",
];
const integers = [
  "iat",
  "exp",
  "generation",
  "approval_revision",
  "offline_not_after",
];
export const CLAIM_FIELDS = [
  ...ids,
  ...hexes,
  ...keys,
  ...integers,
  "iss",
  "aud",
  "suite",
];
export const CONTEXT_FIELDS = CLAIM_FIELDS.filter(
  (key) => !["iss", "aud", "suite", "iat", "exp", "jti"].includes(key),
);
export const toHex = (bytes) =>
  [...bytes].map((x) => x.toString(16).padStart(2, "0")).join("");
export function raw32(value) {
  requirePairing(typeof value === "string" && /^[0-9a-f]{64}$/u.test(value));
  return Uint8Array.from(value.match(/../gu), (x) => parseInt(x, 16));
}
export function b64u(bytes) {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}
export function unb64u(value) {
  requirePairing(
    typeof value === "string" &&
      /^[A-Za-z0-9_-]+$/u.test(value) &&
      value.length <= 8192,
  );
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
  const bytes = unb64u(value);
  requirePairing(bytes.length === 32);
  return bytes;
}
export function exact(object, fields) {
  requirePairing(
    object &&
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
function enrollmentFields(c) {
  return [
    AUDIENCE,
    SUITE,
    c.organization_id,
    c.installation_id,
    c.installation_key_id,
    c.installation_key_jkt,
    c.agent_id,
    c.agent_identity_key_id,
    c.agent_identity_key_jkt,
    c.account_id,
    c.pairing_id,
    key32(c.agent_key),
    key32(c.brain_key),
    raw32(c.agent_nonce),
    raw32(c.brain_nonce),
    c.generation,
    raw32(c.grant_digest),
    c.approval_id,
    c.approval_revision,
    c.offline_not_after,
  ];
}
export const enrollmentDigest = (c) =>
  digest(lp("OFCA-COMPANION-PAIRING-ENROLLMENT-V1", enrollmentFields(c)));
export function proofMessage(role, challenge, enrollment, keyId) {
  requirePairing(["agent", "brain"].includes(role) && id.test(keyId));
  requirePairing(
    challenge instanceof Uint8Array &&
      challenge.length === 32 &&
      enrollment instanceof Uint8Array &&
      enrollment.length === 32,
  );
  return lp("OFCA-COMPANION-PAIRING-PROOF-V1", [
    "pairing-enrollment",
    AUDIENCE,
    role,
    challenge,
    enrollment,
    keyId,
  ]);
}
export function registrationMessage(
  challenge,
  organization,
  agent,
  keyId,
  jkt,
) {
  requirePairing(
    [organization, agent, keyId].every(
      (v) => typeof v === "string" && id.test(v),
    ),
  );
  key32(jkt);
  requirePairing(challenge instanceof Uint8Array && challenge.length === 32);
  return lp("OFCA-AGENT-IDENTITY-REGISTRATION-V1", [
    organization,
    agent,
    keyId,
    jkt,
    challenge,
  ]);
}
export const grantDigest = (creator, installation) =>
  digest(
    lp("OFCA-COMPANION-PAIRING-GRANTS-V1", [
      "creator_account_binding",
      raw32(creator),
      "installation_grant",
      raw32(installation),
    ]),
  );
export const receiptBinding = (c) =>
  digest(
    lp("OFCA-COMPANION-PAIRING-BINDING-V1", [
      TYPE,
      c.iss,
      c.aud,
      c.iat,
      c.exp,
      c.jti,
      c.suite,
      ...enrollmentFields(c).slice(2),
    ]),
  );
export function validateClaims(c) {
  exact(c, CLAIM_FIELDS);
  requirePairing(c.iss === ISSUER && c.aud === AUDIENCE && c.suite === SUITE);
  for (const k of ids)
    requirePairing(typeof c[k] === "string" && id.test(c[k]));
  for (const k of hexes) raw32(c[k]);
  for (const k of keys) key32(c[k]);
  for (const k of integers)
    requirePairing(Number.isSafeInteger(c[k]) && c[k] >= 0);
  requirePairing(
    c.generation > 0 &&
      c.approval_revision > 0 &&
      c.exp > c.iat &&
      c.exp - c.iat <= 300 &&
      c.offline_not_after >= c.exp,
  );
  requirePairing(
    c.agent_key !== c.brain_key && c.agent_nonce !== c.brain_nonce,
  );
}
// Both objects contain only strings/numbers. Tokenize before JSON.parse to reject
// duplicate names (including escaped spellings) without accepting nested JSON.
export function flatObject(bytes, limit) {
  requirePairing(bytes.length <= limit);
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      bytes,
    );
  } catch {
    throw new PairingFailure();
  }
  const string =
    '"(?:[^"\\\\\\x00-\\x1f]|\\\\(?:["\\\\/bfnrt]|u[0-9a-fA-F]{4}))*"';
  const token = new RegExp(
    `[ \\t\\r\\n]*(${string}|-?(?:0|[1-9][0-9]*)|[{}:,])`,
    "y",
  );
  const tokens = [];
  let pos = 0;
  while (pos < text.length && !/^[ \t\r\n]*$/u.test(text.slice(pos))) {
    token.lastIndex = pos;
    const m = token.exec(text);
    requirePairing(m);
    tokens.push(m[1]);
    pos = token.lastIndex;
  }
  requirePairing(tokens.shift() === "{" && tokens.pop() === "}");
  const result = Object.create(null);
  while (tokens.length) {
    const name = tokens.shift();
    requirePairing(name?.startsWith('"') && tokens.shift() === ":");
    const key = JSON.parse(name);
    requirePairing(!Object.hasOwn(result, key));
    const value = tokens.shift();
    requirePairing(value && !["{", "}", ":", ","].includes(value));
    result[key] = JSON.parse(value);
    if (tokens.length)
      requirePairing(tokens.shift() === "," && tokens.length > 0);
  }
  return result;
}
const ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);
export function lowSignature(signature) {
  requirePairing(signature.length === 64);
  const r = BigInt("0x" + toHex(signature.slice(0, 32))),
    s = BigInt("0x" + toHex(signature.slice(32)));
  requirePairing(r > 0n && r < ORDER && s > 0n && s <= ORDER / 2n);
}
export async function thumbprint(jwk) {
  requirePairing(jwk.kty === "EC" && jwk.crv === "P-256");
  key32(jwk.x);
  key32(jwk.y);
  return b64u(
    await digest(
      enc.encode(
        JSON.stringify({ crv: "P-256", kty: "EC", x: jwk.x, y: jwk.y }),
      ),
    ),
  );
}
export async function loadTrustSet(input) {
  try {
    const t = structuredClone(input);
    exact(t, [
      "environment",
      "issuer",
      "keys",
      "production_usable",
      "profile",
      "transition_sequence",
    ]);
    requirePairing(
      t.environment === "production" &&
        t.production_usable === true &&
        t.issuer === ISSUER &&
        t.profile === "pairing-receipt-v1",
    );
    requirePairing(
      Number.isSafeInteger(t.transition_sequence) &&
        t.transition_sequence >= 1 &&
        Array.isArray(t.keys) &&
        t.keys.length > 0 &&
        t.keys.length <= 32,
    );
    const entries = new Map();
    for (const e of t.keys) {
      exact(e, ["alg", "fixture_only", "jwk", "kid", "purpose", "thumbprint"]);
      exact(e.jwk, ["crv", "kid", "kty", "x", "y"]);
      requirePairing(
        e.alg === "ES256" &&
          e.fixture_only === false &&
          e.purpose === "pairing-receipt" &&
          e.jwk.kid === e.kid &&
          !entries.has(e.kid),
      );
      const jkt = await thumbprint(e.jwk);
      requirePairing(
        jkt === e.thumbprint &&
          e.kid === "pr1." + b64u(unb64u(jkt).slice(0, 16)),
      );
      entries.set(
        e.kid,
        await crypto.subtle.importKey(
          "jwk",
          e.jwk,
          { name: "ECDSA", namedCurve: "P-256" },
          false,
          ["verify"],
        ),
      );
    }
    return Object.freeze({
      sequence: t.transition_sequence,
      lookup: (kid) => entries.get(kid),
    });
  } catch {
    throw new PairingFailure("pairing_trust_refused");
  }
}
export async function verifyReceipt(token, { trust, expected, now, deadline }) {
  try {
    const context = structuredClone(expected);
    requirePairing(typeof token === "string" && token.length <= 8192);
    const parts = token.split(".");
    requirePairing(parts.length === 3);
    const h = flatObject(unb64u(parts[0]), 512),
      c = flatObject(unb64u(parts[1]), 6144),
      signature = unb64u(parts[2]);
    exact(h, ["alg", "kid", "typ"]);
    requirePairing(
      h.alg === "ES256" &&
        h.typ === TYPE &&
        typeof h.kid === "string" &&
        id.test(h.kid),
    );
    lowSignature(signature);
    const key = trust.lookup(h.kid);
    requirePairing(key);
    requirePairing(
      await crypto.subtle.verify(
        { name: "ECDSA", hash: "SHA-256" },
        key,
        signature,
        enc.encode(parts[0] + "." + parts[1]),
      ),
    );
    validateClaims(c);
    requirePairing(
      Number.isSafeInteger(now) &&
        Number.isSafeInteger(deadline) &&
        now >= c.iat &&
        now < c.exp &&
        now < deadline,
    );
    for (const field of CONTEXT_FIELDS)
      requirePairing(c[field] === context[field]);
    return Object.freeze({
      claims: Object.freeze(c),
      binding: await receiptBinding(c),
      issuerKid: h.kid,
      trustSequence: trust.sequence,
    });
  } catch {
    throw new PairingFailure("pairing_receipt_refused");
  }
}
