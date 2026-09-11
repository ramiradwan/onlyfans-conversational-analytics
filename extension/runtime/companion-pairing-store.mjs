import {
  PairingFailure,
  requirePairing,
  b64u,
  toHex,
  key32,
  thumbprint,
  CONTEXT_FIELDS,
  verifyReceipt,
  validateClaims,
  ISSUER,
  AUDIENCE,
  SUITE,
} from "../transport/pairing-contract.mjs";

// This store has no network ingress. Hosted authentication must be supplied by
// the reviewed composition layer; loopback messages cannot call admission.
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
      identity: candidate,
      wrapping,
      epoch: 0,
      pending: null,
      active: null,
      lineages: {},
      trustSequence: 0,
      lastTime: 0,
      consumedJtis: [],
    },
    result: null,
  }));
  async function identity() {
    const state = await read(),
      jwk = await crypto.subtle.exportKey("jwk", state.identity.publicKey);
    return {
      publicJwk: { crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y },
      thumbprint: await thumbprint(jwk),
      privateKey: state.identity.privateKey,
    };
  }
  function now() {
    const n = clock();
    requirePairing(Number.isSafeInteger(n) && n >= 0);
    return n;
  }
  const localFields = [
    "organization_id",
    "installation_id",
    "installation_key_id",
    "installation_key_jkt",
    "agent_id",
    "agent_identity_key_id",
    "agent_identity_key_jkt",
    "account_id",
    "pairing_id",
    "generation",
  ];
  async function begin({ context, deadline, generateNoiseKeypair, signal }) {
    const snapshot = structuredClone(context),
      state = await read(),
      started = now();
    requirePairing(
      typeof snapshot.pairing_id === "string" &&
        /^[0-9a-f]{64}$/u.test(snapshot.pairing_id),
    );
    for (const field of localFields.filter(
      (k) =>
        ![
          "pairing_id",
          "generation",
          "installation_key_jkt",
          "agent_identity_key_jkt",
        ].includes(k),
    ))
      requirePairing(
        typeof snapshot[field] === "string" &&
          /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(snapshot[field]),
      );
    key32(snapshot.installation_key_jkt);
    key32(snapshot.agent_identity_key_jkt);
    requirePairing(
      Number.isSafeInteger(deadline) &&
        deadline > started &&
        deadline - started <= 300,
    );
    const own = await identity();
    requirePairing(snapshot.agent_identity_key_jkt === own.thumbprint);
    const pair = await generateNoiseKeypair();
    requirePairing(
      pair.privateKey instanceof Uint8Array &&
        pair.privateKey.length === 32 &&
        pair.publicKey instanceof Uint8Array &&
        pair.publicKey.length === 32,
    );
    const iv = crypto.getRandomValues(new Uint8Array(12));
    let encrypted;
    try {
      encrypted = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        state.wrapping,
        pair.privateKey,
      );
    } finally {
      pair.privateKey.fill(0);
    }
    const pending = {
      requestId: crypto.randomUUID(),
      epoch: state.epoch,
      deadline,
      started,
      context: Object.fromEntries(localFields.map((f) => [f, snapshot[f]])),
      iv,
      encrypted,
    };
    pending.context.agent_key = b64u(pair.publicKey);
    pending.context.agent_nonce = toHex(
      crypto.getRandomValues(new Uint8Array(32)),
    );
    await transaction(
      "readwrite",
      (current) => {
        requirePairing(
          current.epoch === state.epoch &&
            now() < deadline &&
            now() >= started &&
            started >= current.lastTime,
        );
        requirePairing(!current.pending);
        const floor = current.lineages[snapshot.pairing_id];
        requirePairing(
          Number.isSafeInteger(snapshot.generation) &&
            snapshot.generation >
              Math.max(floor?.highest ?? 0, floor?.revoked ?? 0),
        );
        current.pending = pending;
        current.lastTime = started;
        return { state: current, result: null };
      },
      signal,
    );
    return {
      requestId: pending.requestId,
      context: structuredClone(pending.context),
      deadline,
    };
  }
  async function freeze(requestId, expected, signal) {
    const context = structuredClone(expected);
    return transaction(
      "readwrite",
      (state) => {
        const p = state.pending;
        requirePairing(
          p &&
            !p.frozen &&
            p.requestId === requestId &&
            p.epoch === state.epoch &&
            now() >= p.started &&
            now() < p.deadline,
        );
        for (const [key, value] of Object.entries(p.context))
          requirePairing(context[key] === value);
        const check = {
          ...context,
          iss: ISSUER,
          aud: AUDIENCE,
          suite: SUITE,
          iat: p.started,
          exp: p.deadline,
          jti: "0".repeat(64),
        };
        validateClaims(check);
        p.context = Object.fromEntries(
          CONTEXT_FIELDS.map((k) => [k, context[k]]),
        );
        p.frozen = true;
        return { state, result: structuredClone(p.context) };
      },
      signal,
    );
  }
  async function admit(requestId, token, trust, signal) {
    const before = await read(),
      pending = before.pending;
    requirePairing(pending?.frozen && pending.requestId === requestId);
    const verified = await verifyReceipt(token, {
      trust,
      expected: pending.context,
      now: now(),
      deadline: pending.deadline,
    });
    await transaction(
      "readwrite",
      (state) => {
        const p = state.pending,
          c = verified.claims,
          floor = state.lineages[c.pairing_id] ?? {
            highest: 0,
            revoked: 0,
            jtis: [],
          };
        requirePairing(
          p?.frozen &&
            p.requestId === requestId &&
            p.epoch === state.epoch &&
            state.epoch === before.epoch,
        );
        requirePairing(
          now() >= c.iat &&
            now() >= state.lastTime &&
            now() < c.exp &&
            now() < p.deadline,
        );
        requirePairing(
          c.generation > Math.max(floor.highest, floor.revoked) &&
            !floor.jtis.includes(c.jti) &&
            !state.consumedJtis.includes(c.jti) &&
            verified.trustSequence >= state.trustSequence,
        );
        floor.highest = c.generation;
        floor.jtis.push(c.jti);
        state.consumedJtis.push(c.jti);
        state.lineages[c.pairing_id] = floor;
        state.trustSequence = verified.trustSequence;
        state.lastTime = now();
        state.active = {
          ...verified,
          iv: p.iv,
          encrypted: p.encrypted,
          epoch: state.epoch,
        };
        state.pending = null;
        return { state, result: null };
      },
      signal,
    );
    invalidate();
  }
  async function cancel() {
    invalidate();
    await transaction("readwrite", (state) => {
      requirePairing(Number.isSafeInteger(state.epoch + 1));
      state.epoch++;
      state.pending = null;
      state.active = null;
      return { state, result: null };
    });
  }
  async function revoke(pairingId, generation) {
    requirePairing(
      /^[0-9a-f]{64}$/u.test(pairingId) &&
        Number.isSafeInteger(generation) &&
        generation >= 0,
    );
    invalidate();
    await transaction("readwrite", (state) => {
      const floor = state.lineages[pairingId] ?? {
        highest: 0,
        revoked: 0,
        jtis: [],
      };
      floor.revoked = Math.max(floor.revoked, generation);
      state.lineages[pairingId] = floor;
      if (
        state.active?.claims.pairing_id === pairingId &&
        state.active.claims.generation <= floor.revoked
      )
        state.active = null;
      if (
        state.pending?.context.pairing_id === pairingId &&
        state.pending.context.generation <= floor.revoked
      )
        state.pending = null;
      return { state, result: null };
    });
  }
  async function sessionMaterial({ accountId, trustSequence, signal }) {
    requirePairing(!signal?.aborted);
    const state = await transaction("readwrite", (current) => {
        const time = now();
        requirePairing(time >= current.lastTime);
        current.lastTime = time;
        return { state: current, result: current };
      }),
      active = state.active,
      n = now();
    requirePairing(
      active &&
        active.claims.account_id === accountId &&
        active.epoch === state.epoch &&
        trustSequence === state.trustSequence,
    );
    requirePairing(
      n >= active.claims.iat &&
        n < active.claims.offline_not_after &&
        active.claims.generation >
          state.lineages[active.claims.pairing_id].revoked,
    );
    let privateKey;
    try {
      privateKey = new Uint8Array(
        await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: active.iv },
          state.wrapping,
          active.encrypted,
        ),
      );
    } catch {
      throw new PairingFailure("pairing_key_refused");
    }
    const after = await read();
    if (
      signal?.aborted ||
      after.epoch !== state.epoch ||
      after.trustSequence !== state.trustSequence ||
      after.active?.claims.jti !== active.claims.jti ||
      now() < n ||
      now() >= active.claims.offline_not_after
    ) {
      privateKey.fill(0);
      throw new PairingFailure();
    }
    return {
      privateKey,
      publicKey: key32(active.claims.agent_key),
      peerKey: key32(active.claims.brain_key),
      binding: new Uint8Array(active.binding),
      offlineNotAfter: active.claims.offline_not_after,
      generation: active.claims.generation,
      pairingId: active.claims.pairing_id,
    };
  }
  return Object.freeze({
    identity: payloadFree(identity),
    begin: payloadFree(begin),
    freeze: payloadFree(freeze),
    admit: payloadFree(admit),
    cancel: payloadFree(cancel),
    revoke: payloadFree(revoke),
    sessionMaterial: payloadFree(sessionMaterial),
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
    try { return await operation(...args); }
    catch (error) {
      throw error instanceof PairingFailure ? error : new PairingFailure("pairing_storage_refused");
    }
  };
}
export const openPairingStore = payloadFree(openPairingStoreImpl);
