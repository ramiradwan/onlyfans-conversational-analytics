import { openPairingStore } from "../runtime/companion-pairing-store.mjs";
import { loadGrantTrustSet } from "../transport/grant-verifier.mjs";
import {
  PairingFailure,
  b64u,
  encodeMessage,
  grantDigest,
  normalizeSignature,
  pairingDigest,
  proofMessage,
  transcriptOf,
  verifyProof,
} from "../transport/pairing-contract.mjs";
import vector from "../../contracts/companion-pairing-v1/vector.json" with { type: "json" };
import trustSet from "../../contracts/companion-pairing-v1/trust-set.json" with { type: "json" };

const NOW = vector.now,
  ACCOUNT = vector.detected_account_id,
  INSTALLATION = vector.expected.identity.installation_id;
const encoder = new TextEncoder();
const check = (value, label) => {
  if (!value) throw new Error(label);
};
async function refused(fn, code) {
  let error;
  try {
    await fn();
  } catch (e) {
    error = e;
  }
  check(error, "expected_refusal");
  if (code) check(error instanceof PairingFailure && error.code === code, `expected_${code}`);
}
const noiseKeypair = async () => ({
  privateKey: new Uint8Array(32).fill(9),
  publicKey: new Uint8Array(32).fill(8),
});

const P256_ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);

// The contract publishes no private key: a fixture scalar follows from its
// published label under the contract's test-fixture derivation.
async function fixturePrivateJwk(label, publicJwk) {
  const material = new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      encoder.encode(`OFCA TEST VECTORS ONLY - NEVER PRODUCTION - ${label}`),
    ),
  );
  let value = 0n;
  for (const byte of material) value = (value << 8n) | BigInt(byte);
  const scalar = ((value % (P256_ORDER - 1n)) + 1n).toString(16).padStart(64, "0");
  const bytes = Uint8Array.from(scalar.match(/../gu), (pair) => Number.parseInt(pair, 16));
  return { ...publicJwk, d: b64u(bytes) };
}

// Test Brain: the vendored installation key signs offers over fresh Agent
// requests. Its scalar is reconstructed from the published fixture label.
async function brain() {
  const key = await crypto.subtle.importKey(
    "jwk",
    await fixturePrivateJwk(vector.fixture_labels.installation_key, vector.offer.installation_jwk),
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"],
  );
  const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
  async function offer(request, generation) {
    const random = () => b64u(crypto.getRandomValues(new Uint8Array(32)));
    const body = {
      type: "pair.offer",
      pairing_id: random(),
      generation,
      creator_account_id: ACCOUNT,
      brain_noise_key: random(),
      brain_nonce: random(),
      installation_jwk: vector.offer.installation_jwk,
      installation_grant: vector.offer.installation_grant,
      creator_account_binding: vector.offer.creator_account_binding,
    };
    const grants = await grantDigest(body.creator_account_binding, body.installation_grant);
    const digest = await pairingDigest(
      await transcriptOf(request, body, vector.expected.identity, grants),
    );
    const signature = new Uint8Array(
      await crypto.subtle.sign(
        { name: "ECDSA", hash: "SHA-256" },
        key,
        proofMessage("brain", digest),
      ),
    );
    return encoder.encode(encodeMessage({ ...body, brain_proof: b64u(normalizeSignature(signature)) }));
  }
  return { trust, offer };
}
async function pairOnce(store, peer, generation) {
  const pending = await store.begin({
    agentInstallationId: vector.request.agent_installation_id,
    generateNoiseKeypair: noiseKeypair,
    deadline: NOW + 300,
  });
  const accepted = await store.acceptOffer(
    pending.requestId,
    await peer.offer(pending.request, generation),
    { trust: peer.trust, detectedAccountId: ACCOUNT },
  );
  return { pending, accepted };
}

export async function runStorageScenario(stage, expectedIdentity) {
  let time = NOW;
  const store = await openPairingStore({ clock: () => time });
  try {
    if (stage === "seed") {
      const peer = await brain(),
        identity = await store.identity();
      await refused(() => crypto.subtle.exportKey("jwk", identity.privateKey));
      const { pending, accepted } = await pairOnce(store, peer, 7);
      check(/^[0-9]{6}$/u.test(accepted.comparisonCode), "comparison_code");
      const material = await store.sessionMaterial({
        accountId: ACCOUNT,
        requestId: pending.requestId,
      });
      check(
        await verifyProof(identity.publicJwk, "agent", material.pairingDigest, accepted.confirm.agent_proof),
        "agent_proof",
      );
      check(material.privateKey.every((x) => x === 9), "wrapped_key_roundtrip");
      material.privateKey.fill(0);
      check((await store.status()).paired === false, "pin_before_commit");
      await store.commit(material.commit);
      check((await store.status()).paired === true, "pin_after_commit");
      return { identity: identity.thumbprint };
    }
    const identity = await store.identity();
    check(identity.thumbprint === expectedIdentity, "identity_changed_after_restart");
    await refused(() => crypto.subtle.exportKey("jwk", identity.privateKey));
    // The pin carries no lease; session validity comes from the grants at authorization.
    time = NOW + 30 * 86400;
    const material = await store.sessionMaterial({ accountId: ACCOUNT });
    check(material.commit === null && material.generation === 7, "pinned_material");
    material.privateKey.fill(0);
    await refused(
      () => store.sessionMaterial({ accountId: "creator-account-pairing-002" }),
      "pairing_account_refused",
    );
    await refused(
      () =>
        store.begin({
          agentInstallationId: vector.request.agent_installation_id,
          generateNoiseKeypair: noiseKeypair,
          deadline: time + 300,
        }),
      "pairing_state_refused",
    );
    let closed = false;
    store.onInvalidate(() => {
      closed = true;
    });
    await store.forget();
    check(closed, "forget_did_not_close");
    await refused(() => store.sessionMaterial({ accountId: ACCOUNT }), "pairing_state_refused");
    const status = await store.status();
    check(!status.paired && status.highWater[INSTALLATION] === 7, "high_water_after_forget");
    return { restart: true, noPairingLease: true, accountBound: true, singlePin: true, forget: true };
  } finally {
    store.close();
  }
}

export async function runRaceScenario() {
  const peer = await brain(),
    store = await openPairingStore({ name: "pairing-races", clock: () => NOW });
  try {
    // Cancellation during offer verification fences the pending record.
    const a = await store.begin({
      agentInstallationId: vector.request.agent_installation_id,
      generateNoiseKeypair: noiseKeypair,
      deadline: NOW + 300,
    });
    const racingTrust = {
      lookup(kid) {
        void store.cancel();
        return peer.trust.lookup(kid);
      },
    };
    await refused(
      async () =>
        store.acceptOffer(a.requestId, await peer.offer(a.request, 1), {
          trust: racingTrust,
          detectedAccountId: ACCOUNT,
        }),
      "pairing_state_refused",
    );
    await refused(
      () => store.sessionMaterial({ accountId: ACCOUNT, requestId: a.requestId }),
      "pairing_state_refused",
    );

    // A newer attempt supersedes the older one.
    const old = await store.begin({
      agentInstallationId: vector.request.agent_installation_id,
      generateNoiseKeypair: noiseKeypair,
      deadline: NOW + 300,
    });
    const b = await pairOnce(store, peer, 3);
    await refused(
      async () =>
        store.acceptOffer(old.requestId, await peer.offer(old.request, 4), {
          trust: peer.trust,
          detectedAccountId: ACCOUNT,
        }),
      "pairing_state_refused",
    );

    // Two commits of one probation session: exactly one pins.
    const material = await store.sessionMaterial({
      accountId: ACCOUNT,
      requestId: b.pending.requestId,
    });
    material.privateKey.fill(0);
    const results = await Promise.allSettled([
      store.commit(material.commit),
      store.commit(material.commit),
    ]);
    check(results.filter((x) => x.status === "fulfilled").length === 1, "double_commit");

    // The generation high-water survives forget.
    await store.forget();
    await refused(() => pairOnce(store, peer, 3), "pairing_generation_refused");
    await store.cancel();
    await pairOnce(store, peer, 4);
    await store.cancel();

    // A root with another format is refused and left as found.
    const foreign = { format: "ofca-companion-pairing/v0", marker: "unchanged" };
    await putRoot("pairing-format", foreign);
    await refused(() => openPairingStore({ name: "pairing-format" }), "pairing_storage_refused");
    const after = await getRoot("pairing-format");
    check(after?.format === foreign.format && after.marker === foreign.marker, "foreign_state_overwritten");
    return {
      cancellationFence: true,
      supersededAttemptRefused: true,
      singleCommit: true,
      generationHighWaterSurvivesForget: true,
      unsupportedFormatRefused: true,
    };
  } finally {
    store.close();
  }
}

function rawDatabase(name) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name, 1);
    request.onupgradeneeded = () => request.result.createObjectStore("state");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function rootOp(name, mode, op) {
  const db = await rawDatabase(name);
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("state", mode),
        request = op(tx.objectStore("state"));
      tx.oncomplete = () => resolve(request.result);
      tx.onabort = tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}
const putRoot = (name, value) => rootOp(name, "readwrite", (s) => s.put(value, "root"));
const getRoot = (name) => rootOp(name, "readonly", (s) => s.get("root"));
