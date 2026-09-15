import {
  PAIRING_WINDOW_SECONDS,
  PairingFailure,
  b64u,
  key32,
  requirePairing,
  thumbprint,
  verifyOffer,
} from "../transport/pairing-contract.mjs";
import { signPairingProof } from "./companion-agent-identity.mjs";

export const PAIRING_STORE_FORMAT = "ofca-companion-pairing/v1";
const ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;
const floor = (highWater, installationId) =>
  Object.hasOwn(highWater, installationId) ? highWater[installationId] : 0;

// Agent pairing state: one pending pairing, at most one pin, and the admitted
// generation high-water per Brain installation. The store has no network
// ingress; the composition layer carries pairing frames and session records.
async function openPairingStoreImpl({
  indexedDB = globalThis.indexedDB,
  name = "ofca-companion-pairing-v1",
  clock = () => Math.floor(Date.now() / 1000),
} = {}) {
  const db = await new Promise((resolve, reject) => {
    let refused = false;
    const fail = () => {
      refused = true;
      clearTimeout(timer);
      reject(new PairingFailure("pairing_storage_refused"));
    };
    const timer = setTimeout(fail, 5000);
    const request = indexedDB.open(name, 1);
    request.onupgradeneeded = () => {
      if (refused) request.transaction.abort();
      else request.result.createObjectStore("state");
    };
    request.onsuccess = () => {
      clearTimeout(timer);
      if (refused) request.result.close();
      else resolve(request.result);
    };
    request.onerror = request.onblocked = fail;
  });
  const listeners = new Set();
  const invalidate = () => {
    for (const close of listeners) {
      try {
        close();
      } catch {}
    }
    listeners.clear();
  };
  db.onversionchange = () => {
    invalidate();
    db.close();
  };
  function transaction(mode, update, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new PairingFailure("pairing_cancelled"));
        return;
      }
      let tx, result, error;
      try {
        tx = db.transaction("state", mode);
      } catch {
        reject(new PairingFailure("pairing_storage_refused"));
        return;
      }
      const abort = () => {
        try {
          tx.abort();
        } catch {}
      };
      const expires = performance.now() + 5000;
      const timer = setTimeout(abort, 5000);
      signal?.addEventListener("abort", abort, { once: true });
      const clean = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      };
      tx.oncomplete = () => {
        clean();
        resolve(result);
      };
      tx.onabort = tx.onerror = () => {
        clean();
        reject(error ?? new PairingFailure("pairing_storage_refused"));
      };
      const store = tx.objectStore("state"),
        read = store.get("root");
      read.onsuccess = () => {
        try {
          requirePairing(!signal?.aborted && performance.now() < expires);
          const state = read.result;
          requirePairing(
            state === undefined || state?.format === PAIRING_STORE_FORMAT,
            "pairing_storage_refused",
          );
          const outcome = update(state);
          requirePairing(!outcome?.then); // No asynchronous work inside IndexedDB callbacks.
          result = outcome.result;
          if (mode === "readwrite") store.put(outcome.state, "root");
        } catch (e) {
          error =
            e instanceof PairingFailure
              ? e
              : new PairingFailure("pairing_storage_refused");
          abort();
        }
      };
    });
  }
  const read = () => transaction("readonly", (state) => ({ result: state }));
  // CryptoKey structured cloning preserves non-exportability across worker restarts.
  const candidate = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const wrapping = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  await transaction("readwrite", (state) => ({
    state: state ?? {
      format: PAIRING_STORE_FORMAT,
      identity: candidate,
      wrapping,
      epoch: 0,
      pending: null,
      pin: null,
      highWater: {},
    },
    result: null,
  }));
  async function identity() {
    const state = await read(),
      jwk = await crypto.subtle.exportKey("jwk", state.identity.publicKey),
      publicJwk = { crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y };
    return {
      publicJwk,
      thumbprint: await thumbprint(publicJwk),
      privateKey: state.identity.privateKey,
    };
  }
  function now() {
    const n = clock();
    requirePairing(Number.isSafeInteger(n) && n >= 0);
    return n;
  }
  const clone = (value) => structuredClone(value);

  /** Start a pairing attempt with a fresh Noise key and nonce; returns pair.request. */
  async function begin({ agentInstallationId, generateNoiseKeypair, deadline, signal }) {
    const state = await read(),
      started = now();
    requirePairing(typeof agentInstallationId === "string" && ID.test(agentInstallationId));
    requirePairing(
      Number.isSafeInteger(deadline) &&
        deadline > started &&
        deadline - started <= PAIRING_WINDOW_SECONDS,
    );
    requirePairing(!state.pin, "pairing_state_refused");
    const own = await identity();
    const pair = await generateNoiseKeypair();
    let encrypted, iv;
    try {
      requirePairing(
        pair.privateKey instanceof Uint8Array &&
          pair.privateKey.length === 32 &&
          pair.publicKey instanceof Uint8Array &&
          pair.publicKey.length === 32,
      );
      iv = crypto.getRandomValues(new Uint8Array(12));
      encrypted = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        state.wrapping,
        pair.privateKey,
      );
    } finally {
      pair.privateKey?.fill?.(0);
    }
    const request = {
      type: "pair.request",
      agent_installation_id: agentInstallationId,
      agent_identity_jwk: own.publicJwk,
      agent_noise_key: b64u(pair.publicKey),
      agent_nonce: b64u(crypto.getRandomValues(new Uint8Array(32))),
    };
    const pending = {
      requestId: crypto.randomUUID(),
      epoch: state.epoch,
      started,
      deadline,
      request,
      iv,
      encrypted,
      offer: null,
    };
    await transaction(
      "readwrite",
      (current) => {
        const time = now();
        requirePairing(
          current.epoch === state.epoch && !current.pin && time >= started && time < deadline,
          "pairing_state_refused",
        );
        current.pending = pending;
        return { state: current, result: null };
      },
      signal,
    );
    invalidate();
    return { requestId: pending.requestId, request: clone(request), deadline };
  }

  /**
   * Verify pair.offer for the pending attempt and record it; returns pair.confirm
   * and the comparison code.
   */
  async function acceptOffer(requestId, frame, { trust, detectedAccountId, signal }) {
    const before = await read(),
      p = before.pending;
    requirePairing(
      p && p.requestId === requestId && !p.offer && p.epoch === before.epoch && now() < p.deadline,
      "pairing_state_refused",
    );
    const verified = await verifyOffer(p.request, frame, {
      trust,
      detectedAccountId,
      highWater: before.highWater,
      now: now(),
    });
    const agentProof = await signPairingProof(
      { privateKey: before.identity.privateKey },
      verified.pairingDigest,
    );
    const { installation_id } = verified.identity;
    await transaction(
      "readwrite",
      (state) => {
        const current = state.pending;
        requirePairing(
          current?.requestId === requestId &&
            !current.offer &&
            current.epoch === state.epoch &&
            state.epoch === before.epoch &&
            !state.pin &&
            now() < current.deadline,
          "pairing_state_refused",
        );
        requirePairing(
          verified.generation > floor(state.highWater, installation_id),
          "pairing_generation_refused",
        );
        current.offer = {
          pairingId: verified.pairingId,
          generation: verified.generation,
          identity: { ...verified.identity },
          brainNoiseKey: b64u(verified.brainNoiseKey),
          pairingDigest: verified.pairingDigest.slice(),
          grantDigest: verified.grantDigest.slice(),
          comparisonCode: verified.comparisonCode,
        };
        return { state, result: null };
      },
      signal,
    );
    return {
      confirm: { type: "pair.confirm", pairing_id: verified.pairingId, agent_proof: agentProof },
      comparisonCode: verified.comparisonCode,
      identity: { ...verified.identity },
    };
  }

  /**
   * Key material for one KK session: the pin, or with requestId the verified
   * pending pairing, whose session commits the pin.
   */
  async function sessionMaterial({ accountId, requestId, signal }) {
    requirePairing(!signal?.aborted);
    const state = await read(),
      p = state.pending;
    let source;
    if (requestId !== undefined) {
      requirePairing(
        p?.offer && p.requestId === requestId && p.epoch === state.epoch && now() < p.deadline,
        "pairing_state_refused",
      );
      source = { ...p.offer, iv: p.iv, encrypted: p.encrypted, commit: { requestId, epoch: state.epoch } };
    } else {
      requirePairing(state.pin, "pairing_state_refused");
      source = { ...state.pin, commit: null };
    }
    requirePairing(source.identity.creator_account_id === accountId, "pairing_account_refused");
    let privateKey;
    try {
      privateKey = new Uint8Array(
        await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: source.iv },
          state.wrapping,
          source.encrypted,
        ),
      );
    } catch {
      throw new PairingFailure("pairing_key_refused");
    }
    const after = await read(),
      still =
        requestId !== undefined
          ? after.pending?.requestId === requestId && after.pending.offer
          : after.pin?.pairingId === source.pairingId && after.pin.generation === source.generation;
    if (signal?.aborted || after.epoch !== state.epoch || !still) {
      privateKey.fill(0);
      throw new PairingFailure("pairing_state_refused");
    }
    return {
      privateKey,
      peerKey: key32(source.brainNoiseKey),
      pairingDigest: new Uint8Array(source.pairingDigest),
      identity: { ...source.identity },
      pairingId: source.pairingId,
      generation: source.generation,
      commit: source.commit,
    };
  }

  /** Commit the pending pairing as the pin after its first session authorizes. */
  async function commit(token, signal, controls = {}) {
    await transaction(
      "readwrite",
      (state) => {
        controls.assertCurrent?.();
        const p = state.pending;
        requirePairing(
          token &&
            p?.offer &&
            p.requestId === token.requestId &&
            p.epoch === token.epoch &&
            state.epoch === token.epoch &&
            !state.pin &&
            now() < p.deadline,
          "pairing_state_refused",
        );
        const { installation_id } = p.offer.identity;
        requirePairing(
          p.offer.generation > floor(state.highWater, installation_id),
          "pairing_generation_refused",
        );
        state.pin = {
          ...p.offer,
          agentInstallationId: p.request.agent_installation_id,
          iv: p.iv,
          encrypted: p.encrypted,
          committedAt: now(),
        };
        state.highWater[installation_id] = p.offer.generation;
        state.pending = null;
        return { state, result: null };
      },
      signal,
    );
  }

  /** Drop the pending attempt; the pin, if any, is unchanged. */
  async function cancel() {
    await transaction("readwrite", (state) => {
      if (state.pending) invalidate();
      state.pending = null;
      return { state, result: null };
    });
  }

  /** Forget the companion: pin and pending state go, the high-water stays. */
  async function forget() {
    invalidate();
    await transaction("readwrite", (state) => {
      requirePairing(Number.isSafeInteger(state.epoch + 1));
      state.epoch++;
      state.pending = null;
      state.pin = null;
      return { state, result: null };
    });
  }

  async function status() {
    const state = await read();
    return {
      paired: Boolean(state.pin),
      pending: Boolean(state.pending),
      identity: state.pin ? { ...state.pin.identity } : null,
      highWater: { ...state.highWater },
    };
  }

  return Object.freeze({
    identity: payloadFree(identity),
    begin: payloadFree(begin),
    acceptOffer: payloadFree(acceptOffer),
    sessionMaterial: payloadFree(sessionMaterial),
    commit: payloadFree(commit),
    cancel: payloadFree(cancel),
    forget: payloadFree(forget),
    status: payloadFree(status),
    onInvalidate(close) {
      listeners.add(close);
      return () => listeners.delete(close);
    },
    close() {
      invalidate();
      db.close();
    },
  });
}

function payloadFree(operation) {
  return async (...args) => {
    try {
      return await operation(...args);
    } catch (error) {
      throw error instanceof PairingFailure ? error : new PairingFailure("pairing_storage_refused");
    }
  };
}
export const openPairingStore = payloadFree(openPairingStoreImpl);
