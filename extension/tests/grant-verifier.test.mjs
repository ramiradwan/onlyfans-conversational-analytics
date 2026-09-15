import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import {
  GrantTrustError,
  jwkThumbprint,
  loadGrantTrustSet,
  verifyGrant,
} from "../transport/grant-verifier.mjs";

const contracts = new URL("../../contracts/grant-profile-v1/", import.meta.url);
const readJson = async (url) => JSON.parse(await readFile(url, "utf8"));
const fixtureTrust = await readJson(new URL("keys/trust-set.json", contracts));
const productionTrust = await readJson(
  new URL("../production/grant-profile-v1/trust-set.json", contracts),
);
const encoder = new TextEncoder();
const ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);

const contextOf = (data) => ({
  expectedGrantType: data.expected_grant_type,
  expectedAudience: data.expected_audience,
  expectedOrganizationId: data.expected_organization_id,
  expectedInstallationId: data.expected_installation_id,
  expectedInstallationKeyId: data.expected_installation_key_id,
  expectedInstallationKeyJkt: data.expected_installation_key_jkt,
  expectedSubject: data.expected_subject,
  verifierTime: data.fixed_verifier_time,
  tombstones: data.tombstones,
});

const cases = [];
for (const grantType of ["creator_account_binding", "installation_grant"])
  for (const name of (await readdir(new URL(`${grantType}/`, contracts))).sort())
    cases.push([grantType, name, new URL(`${grantType}/${name}/`, contracts)]);

test("the vector set covers both installation-binding grant types", () => {
  assert.equal(cases.length, 32);
});

for (const [grantType, name, dir] of cases)
  test(`grant-profile-v1 ${grantType}/${name}`, async () => {
    const trust = await loadGrantTrustSet(fixtureTrust, { allowNonProduction: true });
    const context = contextOf(await readJson(new URL("verification-context.json", dir)));
    const token = (await readFile(new URL("token.jws", dir), "ascii")).trim();
    assert.deepEqual(
      { ...(await verifyGrant(token, context, trust)) },
      await readJson(new URL("expected.json", dir)),
    );
  });

test("trust sets not marked production-usable are refused by default", async () => {
  await assert.rejects(loadGrantTrustSet(fixtureTrust), GrantTrustError);
  await loadGrantTrustSet(productionTrust);
});

test("malformed trust sets are refused", async () => {
  const entry = productionTrust.keys[0];
  for (const input of [
    { ...productionTrust, keys: [] },
    { ...productionTrust, keys: [entry, entry] },
    { ...productionTrust, keys: [{ ...entry, purpose: "pairing" }] },
    { ...productionTrust, keys: [{ ...entry, purpose: "membership" }] },
    { ...productionTrust, keys: [{ ...entry, thumbprint: fixtureTrust.keys[0].thumbprint }] },
    { ...productionTrust, keys: [{ ...entry, jwk: { ...entry.jwk, extra: "x" } }] },
    { ...productionTrust, keys: [{ ...entry, jwk: { ...entry.jwk, x: entry.jwk.y } }] },
  ])
    await assert.rejects(loadGrantTrustSet(input), GrantTrustError);
});

const b64u = (bytes) =>
  Buffer.from(bytes).toString("base64url");
const canonical = (value) =>
  encoder.encode(
    JSON.stringify(
      Object.fromEntries(Object.keys(value).sort().map((k) => [k, value[k]])),
    ),
  );

const signer = await (async () => {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  const bare = { crv: "P-256", kty: "EC", x: jwk.x, y: jwk.y };
  const digest = await jwkThumbprint(bare);
  const kid = `bc1.ib.${b64u(digest.slice(0, 16))}`;
  const trust = await loadGrantTrustSet({
    production_usable: true,
    profile: "test",
    keys: [{ purpose: "installation-binding", jwk: { ...bare, kid }, thumbprint: b64u(digest) }],
  });
  return { pair, kid, trust };
})();

async function sign(payload, { headerBytes, payloadBytes, highS = false } = {}) {
  const header = { alg: "ES256", kid: signer.kid, typ: "urn:bridge-clean:grant:installation:v1" };
  const input = `${b64u(headerBytes ?? canonical(header))}.${b64u(payloadBytes ?? canonical(payload))}`;
  const raw = new Uint8Array(
    await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, signer.pair.privateKey, encoder.encode(input)),
  );
  let s = BigInt("0x" + Buffer.from(raw.slice(32)).toString("hex"));
  if (s > ORDER / 2n !== highS) s = ORDER - s;
  raw.set(Buffer.from(s.toString(16).padStart(64, "0"), "hex"), 32);
  return `${input}.${b64u(raw)}`;
}

const base = {
  aud: "urn:bridge-clean:local-brain:installation",
  exp: 1_786_924_800,
  grant_type: "installation_grant",
  iat: 1_784_332_800,
  installation_id: "installation-1",
  installation_key_id: "ik1.key",
  installation_key_jkt: "jkt",
  iss: "urn:bridge-clean:commercial-control-plane",
  jti: "0198a1b2-c3d4-7100-8000-000000000001",
  nbf: 1_784_332_800,
  organization_id: "organization-1",
  profile: "urn:bridge-clean:grant-profile:v1",
  sub: "installation:installation-1",
};
const context = {
  expectedGrantType: "installation_grant",
  expectedAudience: base.aud,
  expectedOrganizationId: base.organization_id,
  expectedInstallationId: base.installation_id,
  expectedInstallationKeyId: base.installation_key_id,
  expectedInstallationKeyJkt: base.installation_key_jkt,
  expectedSubject: base.sub,
  verifierTime: base.iat + 1,
  tombstones: [],
};
const result = async (token, overrides = {}) =>
  (await verifyGrant(token, { ...context, ...overrides }, signer.trust)).result;

test("a signed synthetic grant is accepted", async () => {
  assert.equal(await result(await sign(base)), "accepted");
});

test("pre-signature result codes match the reference", async () => {
  const header = (fields) => encoder.encode(fields);
  const cases = [
    ["two.segments", "invalid_compact_jws"],
    ["a".repeat(513) + ".x.x", "header_too_large"],
    ["=.e30.e30", "noncanonical_base64url"],
    [`e30.${b64u(new Uint8Array(12_289))}.e30`, "invalid_compact_jws"],
    [await sign(base, { headerBytes: header('{"alg":"ES256","alg":"ES256","kid":"x","typ":"x"}') }), "duplicate_json_member"],
    [await sign(base, { headerBytes: header("{") }), "invalid_json"],
    [await sign(base, { headerBytes: header("[]") }), "schema_invalid"],
    [await sign(base, { payloadBytes: header('{"x":null}') }), "unsupported_json_value"],
    [await sign(base, { payloadBytes: header('{"x":1.5}') }), "unsupported_json_value"],
    [await sign(base, { payloadBytes: header('{"x":NaN}') }), "unsupported_json_value"],
    [await sign(base, { payloadBytes: header('{"x":"\\ud800"}') }), "unsupported_json_value"],
    [await sign(base, { headerBytes: header(`{"typ":"urn:bridge-clean:grant:installation:v1","kid":"${signer.kid}","alg":"ES256"}`) }), "noncanonical_header"],
    [await sign(base, { payloadBytes: header('{"b":1, "a":2}') }), "noncanonical_payload"],
    [await sign(base, { headerBytes: canonical({ alg: "none", kid: signer.kid, typ: "x" }) }), "invalid_header"],
    [await sign(base, { highS: true }), "high_s_signature"],
  ];
  for (const [token, expected] of cases) assert.equal(await result(token), expected, expected);
});

test("signed result codes match the reference", async () => {
  const cases = [
    [{ extra: true }, "schema_invalid"],
    [{ iss: "urn:other" }, "issuer_or_profile_mismatch"],
    [{ jti: "bad" }, "invalid_jti"],
    [{ iat: true }, "invalid_numeric_date"],
    [{ exp: base.exp + 1 }, "invalid_time_contract"],
    [{ organization_id: "-bad" }, "schema_invalid"],
    [{ organization_id: "other" }, "organization_mismatch"],
    [{ installation_id: "other" }, "installation_mismatch"],
    [{ installation_key_jkt: "other" }, "installation_key_mismatch"],
    [{ sub: "installation:other" }, "subject_mismatch"],
    [{ grant_type: "creator_account_binding" }, "grant_type_mismatch"],
    [{ aud: "urn:other" }, "audience_mismatch"],
  ];
  for (const [change, expected] of cases)
    assert.equal(await result(await sign({ ...base, ...change })), expected, expected);
});

test("time states follow the profile lifetime and grace", async () => {
  const token = await sign(base);
  const at = (verifierTime) => verifyGrant(token, { ...context, verifierTime }, signer.trust);
  assert.equal((await at(base.nbf - 61)).result, "not_yet_valid");
  assert.equal((await at(base.nbf - 60)).time_state, "current");
  assert.equal((await at(base.exp - 1)).time_state, "current");
  assert.equal((await at(base.exp)).time_state, "grace");
  assert.equal((await at(base.exp + 604_799)).time_state, "grace");
  assert.equal((await at(base.exp + 604_800)).result, "expired");
});

test("unsupported grant types are refused before parsing", async () => {
  assert.equal(await result(await sign(base), { expectedGrantType: "membership_snapshot" }), "unsupported_grant_type");
});
