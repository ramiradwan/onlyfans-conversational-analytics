// Workflow continuity only. This dedicated key never pairs a companion,
// authenticates a creator, accepts consent, or grants installation authority.
import { b64u, unb64u, digest, toHex, lp, normalizeSignature, publicJwk } from '../transport/pairing-contract.mjs';
import { onboardingHostedOrigin } from './onboarding-release-config.mjs';

export const SETUP_TRANSFER_MESSAGE = 'ofca.setup-transfer.v1';
const PROFILE = 'urn:bridge-clean:onboarding-transfer:v1';
const PROOF = 'urn:bridge-clean:onboarding-proof:v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const UUID7 = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const CODE = /^[0-9A-HJKMNP-TV-Z]{12}$/u;
const MAX_AGE = 30 * 60 * 1000;
const ALARM = 'onboarding-setup-transfer-expiry-v1';
const exact = (value, fields) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === fields.length && fields.every((key) => Object.hasOwn(value, key));
const require = (value) => { if (!value) throw new Error('setup_transfer_unavailable'); };
const encoder = new TextEncoder();

// These closed request objects contain strings/objects only, so sorted keys
// and JSON string encoding are sufficient for their RFC8785 representation.
export function canonicalTransfer(value) {
  if (typeof value === 'string') return JSON.stringify(value);
  require(value !== null && typeof value === 'object' && !Array.isArray(value));
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalTransfer(value[key])}`).join(',')}}`;
}
export function normalizeSetupCode(value) {
  if (typeof value !== 'string' || value.length > 40) return null;
  const code = value.toUpperCase().replace(/[\s-]/gu, '');
  return CODE.test(code) ? code : null;
}
function uuid7(now) {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  require(Number.isSafeInteger(now) && now >= 0 && now < 2 ** 48);
  for (let position = 5; position >= 0; position--) bytes[5 - position] = Math.floor(now / (2 ** (position * 8))) & 255;
  bytes[6] = (bytes[6] & 15) | 112; bytes[8] = (bytes[8] & 63) | 128;
  const hex = toHex(bytes);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function validateRequest(value) {
  require(exact(value, ['profile', 'operation_id', 'purpose', 'setup_code', 'destination'])
    && value.profile === PROFILE && value.purpose === 'resume-onboarding' && UUID7.test(value.operation_id)
    && CODE.test(value.setup_code) && exact(value.destination, ['kind', 'destination_id', 'public_key'])
    && ['browser-extension', 'desktop'].includes(value.destination.kind)
    && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(value.destination.destination_id));
  publicJwk(value.destination.public_key);
}

export async function transferProofBytes(request, challenge) {
  validateRequest(request);
  require(exact(challenge, ['profile', 'challenge', 'expires_at', 'request_digest']) && challenge.profile === PROOF);
  const nonce = unb64u(challenge.challenge); require(nonce.length === 32);
  const requestDigest = await digest(encoder.encode(canonicalTransfer(request)));
  require(toHex(requestDigest) === challenge.request_digest);
  return lp('BRIDGE-CLEAN-ONBOARDING-PROOF-V1', [nonce, 'POST', '/v1/onboarding/transfers:redeem', requestDigest,
    'transfer-redeem', await digest(encoder.encode(canonicalTransfer(request.destination.public_key))),
    'urn:bridge-clean:commercial-control-plane:onboarding']);
}

export async function openSetupTransferStore(indexedDb = globalThis.indexedDB) {
  const db = await new Promise((resolve, reject) => {
    let closed = false;
    const timer = setTimeout(() => { closed = true; reject(new Error('setup_transfer_storage_unavailable')); }, 5000);
    const request = indexedDb.open('ofca-setup-transfer-v1', 1);
    request.onupgradeneeded = () => { if (closed) request.transaction.abort(); else request.result.createObjectStore('state'); };
    request.onerror = request.onblocked = () => { clearTimeout(timer); closed = true; reject(new Error('setup_transfer_storage_unavailable')); };
    request.onsuccess = () => { clearTimeout(timer); if (closed) request.result.close(); else resolve(request.result); };
  });
  db.onversionchange = () => db.close();
  const transaction = (update) => new Promise((resolve, reject) => {
    let result, failure;
    const tx = db.transaction(['state'], 'readwrite');
    const timer = setTimeout(() => { try { tx.abort(); } catch {} }, 5000);
    tx.oncomplete = () => { clearTimeout(timer); resolve(result); };
    tx.onabort = tx.onerror = () => { clearTimeout(timer); reject(failure ?? new Error('setup_transfer_storage_unavailable')); };
    const store = tx.objectStore('state'), read = store.get('pending');
    read.onsuccess = () => {
      try {
        const outcome = update(read.result ?? null); require(!outcome?.then);
        result = outcome.result;
        if (outcome.state === null) store.delete('pending'); else store.put(outcome.state, 'pending');
      } catch (error) { failure = error; tx.abort(); }
    };
  });
  return { transaction, close: () => db.close() };
}

export function createSetupTransferOwner({ store, now = Date.now, scheduleExpiry = () => {} }) {
  let queue = Promise.resolve();
  const serial = (work) => { const pending = queue.then(work); queue = pending.catch(() => undefined); return pending; };
  const current = async () => store.transaction((state) => {
    if (state && (!Number.isSafeInteger(state.created) || !Number.isSafeInteger(state.expires)
      || state.created > now() || state.expires <= now() || state.expires - state.created !== MAX_AGE)) state = null;
    return { state, result: state };
  });
  return {
    prepare(journeyId, setupCode) { return serial(async () => {
      require(UUID.test(journeyId));
      const code = normalizeSetupCode(setupCode); require(code);
      const previous = await current();
      if (previous?.journey_id === journeyId && previous.request.setup_code === code) {
        await scheduleExpiry(previous.expires);
        return structuredClone(previous.request);
      }
      const keys = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
      const exported = await crypto.subtle.exportKey('jwk', keys.publicKey);
      const key = publicJwk({ crv: exported.crv, kty: exported.kty, x: exported.x, y: exported.y });
      const created = now();
      const request = { profile: PROFILE, operation_id: uuid7(created), purpose: 'resume-onboarding', setup_code: code,
        destination: { kind: 'browser-extension', destination_id: crypto.randomUUID(), public_key: key } };
      const state = { version: 1, journey_id: journeyId, created, expires: created + MAX_AGE,
        request, keys, signed_challenges: [] };
      await store.transaction(() => ({ state, result: null }));
      await scheduleExpiry(state.expires);
      return structuredClone(request);
    }); },
    sign(journeyId, request, challenge) { return serial(async () => {
      const state = await current();
      require(state?.journey_id === journeyId && canonicalTransfer(state.request) === canonicalTransfer(request));
      const expires = Date.parse(challenge?.expires_at);
      require(Number.isFinite(expires) && expires > now() && expires - now() <= 65_000);
      const bytes = await transferProofBytes(request, challenge);
      require(state.keys.privateKey.type === 'private' && state.keys.privateKey.extractable === false
        && state.keys.privateKey.algorithm.name === 'ECDSA' && state.keys.privateKey.algorithm.namedCurve === 'P-256');
      // Persist single use before signing. A lost reply needs a fresh challenge.
      await store.transaction((latest) => {
        require(latest?.request.operation_id === request.operation_id && latest.expires > now()
          && latest.signed_challenges.length < 64 && !latest.signed_challenges.includes(challenge.challenge));
        latest.signed_challenges.push(challenge.challenge); return { state: latest, result: null };
      });
      const signature = normalizeSignature(new Uint8Array(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, state.keys.privateKey, bytes)));
      return { challenge: challenge.challenge, signature: b64u(signature) };
    }); },
    resume(journeyId, continuation, intendedCreatorId) { return serial(async () => {
      const state = await current();
      require(state?.journey_id === journeyId && state.signed_challenges.length > 0
        && exact(continuation, ['profile', 'reference', 'return_target', 'expires_at'])
        && continuation.profile === 'urn:bridge-clean:onboarding-continuation:v1'
        && continuation.return_target === 'extension-setup' && /^[A-Za-z0-9_-]{43}$/u.test(continuation.reference)
        && Date.parse(continuation.expires_at) > now() && Date.parse(continuation.expires_at) - now() <= MAX_AGE
        && (intendedCreatorId === null || (typeof intendedCreatorId === 'string'
          && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(intendedCreatorId))));
      // This is only a saved setup choice. Live account, consent, permissions,
      // pairing and commercial authority still come from their original owners.
      await store.transaction((latest) => {
        require(latest?.request.operation_id === state.request.operation_id && latest.expires > now());
        return { state: { ...latest, continuation, intended_creator_id: intendedCreatorId }, result: null };
      });
    }); },
    context(journeyId) { return serial(async () => {
      const state = await current();
      return state?.journey_id === journeyId && state.continuation
        ? { intended_creator_id: state.intended_creator_id, expires_at: state.expires } : null;
    }); },
    prune() { return serial(current); },
  };
}

export function registerSetupTransfer({ chromeApi, workspace, identityBridge, hostedOrigin = onboardingHostedOrigin, openStore = openSetupTransferStore }) {
  let owner, database;
  const getOwner = () => owner ??= openStore().then((store) => {
    database = store;
    return createSetupTransferOwner({ store, scheduleExpiry: (when) => chromeApi.alarms.create(ALARM, { when }) });
  }).catch((error) => { owner = undefined; throw error; });
  const alarmListener = (alarm) => {
    if (alarm.name === ALARM) void getOwner().then((value) => value.prune()).catch(() => undefined);
  };
  chromeApi.alarms.onAlarm.addListener(alarmListener);
  const listener = (message, sender, reply) => {
    if (message?.type !== SETUP_TRANSFER_MESSAGE) return false;
    const run = async () => {
      require(hostedOrigin !== null && encoder.encode(JSON.stringify(message)).byteLength <= 8192);
      const admitted = await workspace.admit(sender);
      const journeyId = admitted.record.journey_id;
      if (exact(message, ['type', 'action']) && message.action === 'context' && admitted.route === 'extension') {
        const hint = await (await getOwner()).context(journeyId);
        const observed = await identityBridge?.currentAccountId() ?? null;
        require((await workspace.admit(sender)).record.journey_id === journeyId);
        return hint === null ? null : { ...hint, current_creator_id: observed };
      }
      if (exact(message, ['type', 'action', 'setup_code']) && message.action === 'prepare' && admitted.route === 'extension') {
        const request = await (await getOwner()).prepare(journeyId, message.setup_code);
        // Re-admit after asynchronous storage/key generation before exposing data.
        require((await workspace.admit(sender)).record.journey_id === journeyId);
        return { journey_id: journeyId, request, hosted_start_url: `${hostedOrigin}/public/onboarding/receive` };
      }
      if (exact(message, ['type', 'action', 'request', 'challenge']) && message.action === 'sign' && admitted.route === 'hosted') {
        const result = await (await getOwner()).sign(journeyId, message.request, message.challenge);
        require((await workspace.admit(sender)).record.journey_id === journeyId);
        return result;
      }
      if (exact(message, ['type', 'action', 'continuation', 'intended_creator_id'])
        && message.action === 'resume' && admitted.route === 'hosted') {
        await (await getOwner()).resume(journeyId, message.continuation, message.intended_creator_id);
        require((await workspace.admit(sender)).record.journey_id === journeyId);
        await chromeApi.storage.session.set({ onboarding_full_intent_v1: journeyId });
        return workspace.navigate(sender, { route: 'extension' });
      }
      if (exact(message, ['type', 'action']) && message.action === 'enter-code' && admitted.route === 'hosted') {
        await chromeApi.storage.session.set({ onboarding_code_entry_v1: journeyId });
        return workspace.navigate(sender, { route: 'extension' });
      }
      throw new Error('setup_transfer_unavailable');
    };
    void run().then((result) => reply({ ok: true, result }), () => reply({ ok: false, code: 'setup_transfer_unavailable' }));
    return true;
  };
  chromeApi.runtime.onMessage.addListener(listener);
  chromeApi.runtime.onMessageExternal?.addListener(listener);
  return { close() {
    chromeApi.runtime.onMessage.removeListener?.(listener);
    chromeApi.runtime.onMessageExternal?.removeListener?.(listener);
    chromeApi.alarms.onAlarm.removeListener?.(alarmListener);
    database?.close();
  } };
}
