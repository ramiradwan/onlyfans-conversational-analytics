import { openPairingStore } from "../runtime/companion-pairing-store.mjs";
import {
  b64u,
  raw32,
  toHex,
  thumbprint,
  unb64u,
  loadTrustSet,
} from "../transport/pairing-contract.mjs";
import vector from "../test-fixtures/pairing/authority-vector.json" with { type: "json" };
const NOW = 1800000000,
  ORDER = BigInt(
    "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
  );
const check = (value, label) => {
  if (!value) throw new Error(label);
};
async function refused(fn) {
  let error;
  try {
    await fn();
  } catch (e) {
    error = e;
  }
  check(error, "expected_refusal");
}
async function authority() {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const publicKey = await crypto.subtle.exportKey("jwk", pair.publicKey),
    jkt = await thumbprint(publicKey),
    kid = "pr1." + b64u(unb64u(jkt).slice(0, 16));
  const jwk = {
    crv: publicKey.crv,
    kty: publicKey.kty,
    x: publicKey.x,
    y: publicKey.y,
    kid,
  };
  const trust = await loadTrustSet({
    environment: "production",
    issuer: vector.receipt_claims.iss,
    production_usable: true,
    profile: "pairing-receipt-v1",
    transition_sequence: 1,
    keys: [
      {
        alg: "ES256",
        fixture_only: false,
        jwk,
        kid,
        purpose: "pairing-receipt",
        thumbprint: jkt,
      },
    ],
  });
  return {
    trust,
    async sign(claims) {
      const head = b64u(
          new TextEncoder().encode(
            JSON.stringify({
              alg: "ES256",
              typ: "ofca-companion-pairing+jwt",
              kid,
            }),
          ),
        ),
        body = b64u(new TextEncoder().encode(JSON.stringify(claims)));
      const signature = new Uint8Array(
        await crypto.subtle.sign(
          { name: "ECDSA", hash: "SHA-256" },
          pair.privateKey,
          new TextEncoder().encode(head + "." + body),
        ),
      );
      const s = BigInt("0x" + toHex(signature.slice(32)));
      if (s > ORDER / 2n)
        signature.set(raw32((ORDER - s).toString(16).padStart(64, "0")), 32);
      return head + "." + body + "." + b64u(signature);
    },
  };
}
async function prepare(store, issuer, generation = 7, pairingId = vector.receipt_claims.pairing_id) {
  const identity = await store.identity();
  const context = {
    ...vector.receipt_claims,
    generation,
    pairing_id: pairingId,
    agent_identity_key_jkt: identity.thumbprint,
  };
  const pending = await store.begin({
    context,
    deadline: NOW + 300,
    generateNoiseKeypair: async () => ({
      privateKey: new Uint8Array(32).fill(9),
      publicKey: new Uint8Array(32).fill(8),
    }),
  });
  const frozen = { ...context, ...pending.context };
  // Only the closed contextual fields are consumed by freeze.
  delete frozen.iss;
  delete frozen.aud;
  delete frozen.suite;
  delete frozen.iat;
  delete frozen.exp;
  delete frozen.jti;
  await store.freeze(pending.requestId, frozen);
  const claims = {
    ...vector.receipt_claims,
    ...frozen,
    jti: generation.toString(16).padStart(2, "0").repeat(32),
  };
  return { pending, token: await issuer.sign(claims), identity };
}
export async function runStorageScenario(stage, expectedIdentity) {
  let time = NOW;
  const store = await openPairingStore({ clock: () => time });
  try {
    if (stage === "seed") {
      const issuer = await authority(),
        { pending, token, identity } = await prepare(store, issuer);
      await refused(() => crypto.subtle.exportKey("jwk", identity.privateKey));
      await store.admit(pending.requestId, token, issuer.trust);
      const material = await store.sessionMaterial({
        accountId: "account-1",
        trustSequence: 1,
      });
      check(
        material.privateKey.every((x) => x === 9),
        "wrapped_key_roundtrip",
      );
      material.privateKey.fill(0);
      return { identity: identity.thumbprint };
    }
    const identity = await store.identity();
    check(
      identity.thumbprint === expectedIdentity,
      "identity_changed_after_restart",
    );
    await refused(() => crypto.subtle.exportKey("jwk", identity.privateKey));
    time = NOW + 301;
    const material = await store.sessionMaterial({
      accountId: "account-1",
      trustSequence: 1,
    });
    material.privateKey.fill(0);
    await refused(() =>
      store.sessionMaterial({ accountId: "other-account", trustSequence: 1 }),
    );
    await refused(() =>
      store.sessionMaterial({ accountId: "account-1", trustSequence: 2 }),
    );
    time = NOW;
    await refused(() =>
      store.sessionMaterial({ accountId: "account-1", trustSequence: 1 }),
    );
    time = NOW + 301;
    let closed = false;
    store.onInvalidate(() => {
      closed = true;
    });
    await store.revoke(vector.receipt_claims.pairing_id, 7);
    check(closed, "revocation_did_not_close");
    await refused(() =>
      store.sessionMaterial({ accountId: "account-1", trustSequence: 1 }),
    );
    return {
      restart: true,
      receiptExpiryIndependent: true,
      rollbackRefused: true,
      revocation: true,
    };
  } finally {
    store.close();
  }
}
export async function runRaceScenario() {
  const issuer = await authority(),
    store = await openPairingStore({ name: "pairing-races", clock: () => NOW });
  try {
    const a = await prepare(store, issuer);
    const substituted = { ...vector.receipt_claims, account_id: "substitute" };
    await refused(() => store.freeze(a.pending.requestId, substituted));
    const racingTrust = {
      ...issuer.trust,
      lookup(kid) {
        void store.cancel();
        return issuer.trust.lookup(kid);
      },
    };
    await refused(() => store.admit(a.pending.requestId, a.token, racingTrust));
    await refused(() =>
      store.sessionMaterial({ accountId: "account-1", trustSequence: 1 }),
    );
    const b = await prepare(store, issuer);
    const results = await Promise.allSettled([
      store.admit(b.pending.requestId, b.token, issuer.trust),
      store.admit(b.pending.requestId, b.token, issuer.trust),
    ]);
    check(
      results.filter((x) => x.status === "fulfilled").length === 1,
      "double_admission",
    );
    await store.cancel();
    await refused(() => prepare(store, issuer, 7));
    const other = await prepare(store, issuer, 7, "ab".repeat(32));
    await refused(() => store.admit(other.pending.requestId, other.token, issuer.trust));
    return {
      cancellationFence: true,
      singleAdmission: true,
      generationRollbackRefused: true,
      crossLineageReplayRefused: true,
    };
  } finally {
    store.close();
  }
}
