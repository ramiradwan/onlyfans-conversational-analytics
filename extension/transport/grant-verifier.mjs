// Offline grant-profile-v1 verification for the two installation-binding grants.
// Check order and result codes follow app/security/grant_verifier.py.

const ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);
const UUIDV7 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;
const B64U = /^[A-Za-z0-9_-]*$/u;
export const GRANT_ISSUER = "urn:bridge-clean:commercial-control-plane";
export const GRANT_PROFILE = "urn:bridge-clean:grant-profile:v1";
const PURPOSE_CODES = Object.freeze({
  "installation-binding": "ib",
  membership: "ms",
  license: "le",
});
export const GRANT_PROFILES = Object.freeze({
  installation_grant: Object.freeze({
    typ: "urn:bridge-clean:grant:installation:v1",
    purpose: "installation-binding",
    lifetime: 2_592_000,
    grace: 604_800,
    extra: Object.freeze([]),
  }),
  creator_account_binding: Object.freeze({
    typ: "urn:bridge-clean:grant:creator-binding:v1",
    purpose: "installation-binding",
    lifetime: 604_800,
    grace: 259_200,
    extra: Object.freeze([
      "approval_id",
      "approval_revision",
      "creator_account_id",
    ]),
  }),
});
const COMMON_CLAIMS = [
  "aud",
  "exp",
  "grant_type",
  "iat",
  "installation_id",
  "installation_key_id",
  "installation_key_jkt",
  "iss",
  "jti",
  "nbf",
  "organization_id",
  "profile",
  "sub",
];
const encoder = new TextEncoder();

export class GrantTrustError extends Error {
  constructor() {
    super("invalid_trust_set");
    this.name = "GrantTrustError";
    this.code = "invalid_trust_set";
  }
}

class Refusal extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

const outcome = (valid, result, timeState = "invalid") =>
  Object.freeze({ valid, result, time_state: timeState });

const codePointLength = (value) =>
  value.length > 32_768 ? Infinity : [...value].length;

function b64uEncode(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "");
}

function b64uDecode(value) {
  if (typeof value !== "string" || !B64U.test(value))
    throw new Refusal("noncanonical_base64url");
  let decoded;
  try {
    decoded = Uint8Array.from(
      atob(
        value.replace(/-/gu, "+").replace(/_/gu, "/") +
          "=".repeat((4 - (value.length % 4)) % 4),
      ),
      (ch) => ch.charCodeAt(0),
    );
  } catch {
    throw new Refusal("noncanonical_base64url");
  }
  if (b64uEncode(decoded) !== value) throw new Refusal("noncanonical_base64url");
  return decoded;
}

// Floats, null, and integers outside the safe range are held as this marker so
// the canonical check can refuse them after parsing, as the reference does.
const UNSUPPORTED = Symbol("unsupported_json_value");

// Strict JSON: Python json.loads grammar with duplicate members refused when an
// object closes.
function strictJson(bytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    throw new Refusal("invalid_json");
  }
  let pos = 0;
  const fail = () => {
    throw new Refusal("invalid_json");
  };
  const space = () => {
    while (pos < text.length && " \t\n\r".includes(text[pos])) pos += 1;
  };
  const literal = (word, value) => {
    if (text.startsWith(word, pos)) {
      pos += word.length;
      return value;
    }
    return fail();
  };
  const string = () => {
    pos += 1;
    let result = "";
    for (;;) {
      if (pos >= text.length) fail();
      const ch = text[pos];
      if (ch === '"') {
        pos += 1;
        return result;
      }
      if (ch.charCodeAt(0) < 0x20) fail();
      if (ch !== "\\") {
        result += ch;
        pos += 1;
        continue;
      }
      const esc = text[pos + 1];
      const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
      if (Object.hasOwn(simple, esc)) {
        result += simple[esc];
        pos += 2;
      } else if (esc === "u" && /^[0-9a-fA-F]{4}$/u.test(text.slice(pos + 2, pos + 6))) {
        result += String.fromCharCode(parseInt(text.slice(pos + 2, pos + 6), 16));
        pos += 6;
      } else fail();
    }
  };
  const number = () => {
    const match = /-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?/uy;
    match.lastIndex = pos;
    const found = match.exec(text);
    if (!found) fail();
    pos = match.lastIndex;
    if (found[1] || found[2]) return UNSUPPORTED;
    const value = Number(found[0]);
    return Number.isSafeInteger(value) ? value : UNSUPPORTED;
  };
  const value = () => {
    space();
    const ch = text[pos];
    if (ch === "{") {
      pos += 1;
      const pairs = [];
      space();
      if (text[pos] === "}") pos += 1;
      else
        for (;;) {
          space();
          if (text[pos] !== '"') fail();
          const key = string();
          space();
          if (text[pos] !== ":") fail();
          pos += 1;
          pairs.push([key, value()]);
          space();
          if (text[pos] === ",") pos += 1;
          else if (text[pos] === "}") {
            pos += 1;
            break;
          } else fail();
        }
      const result = Object.create(null);
      for (const [key, item] of pairs) {
        if (Object.hasOwn(result, key)) throw new Refusal("duplicate_json_member");
        result[key] = item;
      }
      return result;
    }
    if (ch === "[") {
      pos += 1;
      const items = [];
      space();
      if (text[pos] === "]") pos += 1;
      else
        for (;;) {
          items.push(value());
          space();
          if (text[pos] === ",") pos += 1;
          else if (text[pos] === "]") {
            pos += 1;
            break;
          } else fail();
        }
      return items;
    }
    if (ch === '"') return string();
    if (ch === "t") return literal("true", true);
    if (ch === "f") return literal("false", false);
    if (ch === "n") return literal("null", null);
    if (ch === "N") return literal("NaN", UNSUPPORTED);
    if (ch === "I") return literal("Infinity", UNSUPPORTED);
    if (ch === "-" && text[pos + 1] === "I") return literal("-Infinity", UNSUPPORTED);
    return number();
  };
  const result = value();
  space();
  if (pos !== text.length) fail();
  if (!result || typeof result !== "object" || Array.isArray(result))
    throw new Refusal("schema_invalid");
  return result;
}

const isObject = (item) =>
  item !== null && typeof item === "object" && !Array.isArray(item);

function compareCodePoints(a, b) {
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = x[i].codePointAt(0) - y[i].codePointAt(0);
    if (d) return d;
  }
  return x.length - y.length;
}

// Sorted keys, compact separators, non-ASCII kept literal.
function canonicalJson(item) {
  const write = (v) => {
    if (v === null || v === UNSUPPORTED) throw new Refusal("unsupported_json_value");
    if (typeof v === "string") {
      if (!v.isWellFormed()) throw new Refusal("unsupported_json_value");
      return JSON.stringify(v);
    }
    if (typeof v === "boolean") return String(v);
    if (typeof v === "number") return String(v);
    if (Array.isArray(v)) return `[${v.map(write).join(",")}]`;
    if (isObject(v))
      return `{${Object.keys(v)
        .sort(compareCodePoints)
        .map((k) => `${write(k)}:${write(v[k])}`)
        .join(",")}}`;
    throw new Refusal("unsupported_json_value");
  };
  return encoder.encode(write(item));
}

const sameBytes = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

const toBigInt = (bytes) =>
  BigInt("0x" + [...bytes].map((x) => x.toString(16).padStart(2, "0")).join(""));

export async function jwkThumbprint(jwk) {
  const bare = JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y });
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(bare)));
}

const exactKeys = (object, names) =>
  isObject(object) &&
  Object.keys(object).length === names.length &&
  names.every((name) => Object.hasOwn(object, name));

/**
 * Load a packaged trust set. Refuses one not marked production-usable unless
 * allowNonProduction is set, which only test harnesses do.
 */
export async function loadGrantTrustSet(input, { allowNonProduction = false } = {}) {
  try {
    const trust = structuredClone(input);
    if (!isObject(trust)) throw new GrantTrustError();
    if (!allowNonProduction && trust.production_usable !== true) throw new GrantTrustError();
    if (typeof trust.profile !== "string" || !Array.isArray(trust.keys) || !trust.keys.length)
      throw new GrantTrustError();
    const keys = new Map();
    for (const entry of trust.keys) {
      if (!isObject(entry)) throw new GrantTrustError();
      const { purpose, jwk, thumbprint } = entry;
      if (!Object.hasOwn(PURPOSE_CODES, purpose) || typeof thumbprint !== "string")
        throw new GrantTrustError();
      if (!exactKeys(jwk, ["crv", "kid", "kty", "x", "y"]) || jwk.crv !== "P-256" || jwk.kty !== "EC")
        throw new GrantTrustError();
      if (b64uDecode(jwk.x).length !== 32 || b64uDecode(jwk.y).length !== 32)
        throw new GrantTrustError();
      const digest = await jwkThumbprint(jwk);
      const kid = `bc1.${PURPOSE_CODES[purpose]}.${b64uEncode(digest.slice(0, 16))}`;
      if (jwk.kid !== kid || thumbprint !== b64uEncode(digest) || keys.has(kid))
        throw new GrantTrustError();
      const key = await crypto.subtle.importKey(
        "jwk",
        { kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y },
        { name: "ECDSA", namedCurve: "P-256" },
        false,
        ["verify"],
      );
      keys.set(kid, Object.freeze({ purpose, key }));
    }
    return Object.freeze({ lookup: (kid) => (typeof kid === "string" ? keys.get(kid) : undefined) });
  } catch {
    throw new GrantTrustError();
  }
}

/** Decode a grant payload without verifying it, to propose context values. */
export function peekGrantClaims(token) {
  try {
    if (typeof token !== "string" || token.length > 16_384) return null;
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    return strictJson(b64uDecode(parts[1]));
  } catch {
    return null;
  }
}

/**
 * Verify one compact JWS grant. context: expectedGrantType, expectedAudience,
 * expectedOrganizationId, expectedInstallationId, expectedInstallationKeyId,
 * expectedInstallationKeyJkt, expectedSubject, verifierTime, tombstones.
 */
export async function verifyGrant(token, context, trust) {
  const profile = Object.hasOwn(GRANT_PROFILES, context.expectedGrantType)
    ? GRANT_PROFILES[context.expectedGrantType]
    : null;
  if (!profile) return outcome(false, "unsupported_grant_type");
  if (typeof token !== "string" || codePointLength(token) > 16_384 || token.split(".").length !== 3)
    return outcome(false, "invalid_compact_jws");
  const [headerSegment, payloadSegment, signatureSegment] = token.split(".");
  let header, payload, headerBytes, payloadBytes, signature;
  try {
    if (codePointLength(headerSegment) > 512) throw new Refusal("header_too_large");
    headerBytes = b64uDecode(headerSegment);
    payloadBytes = b64uDecode(payloadSegment);
    signature = b64uDecode(signatureSegment);
    if (payloadBytes.length > 12_288) throw new Refusal("payload_too_large");
    header = strictJson(headerBytes);
    payload = strictJson(payloadBytes);
    if (!sameBytes(canonicalJson(header), headerBytes)) return outcome(false, "noncanonical_header");
    if (!sameBytes(canonicalJson(payload), payloadBytes)) return outcome(false, "noncanonical_payload");
  } catch (error) {
    if (error instanceof Refusal) return outcome(false, error.code);
    return outcome(false, "invalid_json");
  }
  if (!exactKeys(header, ["alg", "kid", "typ"]) || header.alg !== "ES256")
    return outcome(false, "invalid_header");
  if (payload.grant_type !== context.expectedGrantType) return outcome(false, "grant_type_mismatch");
  if (header.typ !== profile.typ) return outcome(false, "typ_mismatch");
  if (payload.aud !== context.expectedAudience) return outcome(false, "audience_mismatch");
  const entry = trust?.lookup?.(header.kid);
  if (!entry) return outcome(false, "unknown_kid");
  if (entry.purpose !== profile.purpose) return outcome(false, "wrong_key_purpose");
  if (signature.length === 64 && toBigInt(signature.slice(32)) > ORDER / 2n)
    return outcome(false, "high_s_signature");
  if (!(await verifySignature(entry.key, `${headerSegment}.${payloadSegment}`, signature)))
    return outcome(false, "invalid_signature");
  if (!exactKeys(payload, [...COMMON_CLAIMS, ...profile.extra])) return outcome(false, "schema_invalid");
  if (payload.profile !== GRANT_PROFILE || payload.iss !== GRANT_ISSUER)
    return outcome(false, "issuer_or_profile_mismatch");
  if (typeof payload.jti !== "string" || !UUIDV7.test(payload.jti)) return outcome(false, "invalid_jti");
  if (!["iat", "nbf", "exp"].every((name) => Number.isSafeInteger(payload[name])))
    return outcome(false, "invalid_numeric_date");
  if (payload.iat !== payload.nbf || payload.exp !== payload.iat + profile.lifetime)
    return outcome(false, "invalid_time_contract");
  for (const field of ["organization_id", "installation_id", "installation_key_id"])
    if (typeof payload[field] !== "string" || !ID.test(payload[field])) return outcome(false, "schema_invalid");
  if (payload.organization_id !== context.expectedOrganizationId) return outcome(false, "organization_mismatch");
  if (payload.installation_id !== context.expectedInstallationId) return outcome(false, "installation_mismatch");
  if (
    payload.installation_key_id !== context.expectedInstallationKeyId ||
    payload.installation_key_jkt !== context.expectedInstallationKeyJkt
  )
    return outcome(false, "installation_key_mismatch");
  if (payload.sub !== context.expectedSubject) return outcome(false, "subject_mismatch");
  if (
    context.expectedGrantType === "creator_account_binding" &&
    !(Number.isSafeInteger(payload.approval_revision) && payload.approval_revision > 0)
  )
    return outcome(false, "schema_invalid");
  const now = context.verifierTime;
  if (!Number.isSafeInteger(now)) return outcome(false, "invalid_verifier_time");
  if (now + 60 < payload.nbf) return outcome(false, "not_yet_valid");
  for (const tombstone of context.tombstones ?? []) {
    const effective = Object.hasOwn(tombstone, "effective_at") ? tombstone.effective_at : now + 1;
    if (tombstone.scope_type === "jti" && tombstone.grant_jti === payload.jti && effective <= now)
      return outcome(false, "revoked");
  }
  if (now < payload.exp) return outcome(true, "accepted", "current");
  if (now < payload.exp + profile.grace) return outcome(true, "accepted", "grace");
  return outcome(false, "expired");
}

async function verifySignature(key, signingInput, signature) {
  if (signature.length !== 64) return false;
  const r = toBigInt(signature.slice(0, 32));
  const s = toBigInt(signature.slice(32));
  if (r < 1n || r >= ORDER || s < 1n || s >= ORDER) return false;
  try {
    return await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      key,
      signature,
      encoder.encode(signingInput),
    );
  } catch {
    return false;
  }
}
