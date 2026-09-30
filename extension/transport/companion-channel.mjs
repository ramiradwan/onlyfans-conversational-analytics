import { createCompanionSession } from './companion-noise-session.mjs';
import { key32 } from './pairing-contract.mjs';
import { createFragmentReceiver, fragmentMessage } from './companion-fragments.mjs';

export class CompanionChannelError extends Error {
  constructor(code = 'companion_session_refused') { super(code); this.code = code; }
}
const refused = () => new CompanionChannelError();
const MAX_BUFFERED = 128 * 1024;
export const SESSION_CONTROLS = Object.freeze(new Set(['capture.pause', 'capture.resume', 'companion.revoked']));
const CLOSE_REASONS = new Set([
  'pairing_account_refused', 'pairing_generation_refused', 'pairing_grant_refused',
  'pairing_key_refused', 'pairing_message_invalid', 'pairing_nonce_refused',
  'pairing_proof_refused', 'pairing_state_refused', 'pairing_storage_refused',
  'session_refused', 'wrong_role', 'validation_failed', 'unsupported_version',
  'pre_handshake', 'unauthorized', 'identity_conflict',
  'companion_session_closed', 'companion_session_refused', 'heartbeat_lease_expired',
  'session_timeout', 'credential_unavailable', 'credential_store_failed',
  'agent_stopped', 'malformed_json', 'invalid_frame', 'session_expected', 'duplicate_session',
  'internal_error', 'companion_recovery_backoff',
]);
const LOCAL_CLOSE_REASONS = new Map([
  ['Session establishment timed out', 'session_timeout'],
  ['Agent reconnect credential unavailable', 'credential_unavailable'],
  ['Agent reconnect credential could not be stored', 'credential_store_failed'],
  ['Agent stopped', 'agent_stopped'],
  ['Malformed JSON from Brain', 'malformed_json'],
  ['Invalid protocol frame from Brain', 'invalid_frame'],
  ['Expected agent.session', 'session_expected'],
  ['Duplicate agent.session', 'duplicate_session'],
  ['Session identity conflict', 'identity_conflict'],
]);

export function safeCompanionChannelDiagnostic(value) {
  if (value == null) return null;
  return {
    cause: ['rpc_capacity', 'send_capacity', 'rpc_backlog', 'caller_aborted', 'rpc_timeout',
      'send_failed', 'receive_failed', 'wire_closed', 'local_close', 'closed'].includes(value.cause) ? value.cause : 'other',
    ...Object.fromEntries(['pendingRpcs', 'abandonedRpcs', 'queuedSends'].map(key => [key,
      Number.isSafeInteger(value[key]) && value[key] >= 0 ? value[key] : null])),
  };
}

export function safeCompanionCloseReason(reason) {
  if (reason === 'Agent heartbeat lease expired') return 'heartbeat_lease_expired';
  if (LOCAL_CLOSE_REASONS.has(reason)) return LOCAL_CLOSE_REASONS.get(reason);
  return reason === null || reason === undefined ? null : CLOSE_REASONS.has(reason) ? reason : 'other';
}

export async function openLoopbackSocket(url, { webSocketFactory = (value) => new WebSocket(value), signal, text = false } = {}) {
  const socket = webSocketFactory(url);
  socket.binaryType = 'arraybuffer';
  let pending = null, stopped = false, closeReason = null, closeCode = null;
  const queue = [];
  const listeners = new Set();
  let openedResolve, openedReject;
  const opened = new Promise((resolve, reject) => { openedResolve = resolve; openedReject = reject; });
  const close = () => {
    if (stopped) return;
    stopped = true;
    signal?.removeEventListener('abort', close);
    clearTimeout(openTimer);
    openedReject(refused());
    pending?.reject(refused());
    pending = null;
    queue.length = 0;
    try { socket.close(4008, 'companion_session_closed'); } catch {}
    for (const listener of listeners) listener();
  };
  const openTimer = setTimeout(close, 10_000);
  socket.onopen = () => { clearTimeout(openTimer); openedResolve(); };
  socket.onerror = close;
  // Keeps the peer's refusal code when the peer ends the connection first.
  socket.onclose = (event) => {
    if (!stopped && Number.isInteger(event?.code)) closeCode = event.code;
    if (!stopped) closeReason = safeCompanionCloseReason(event?.reason);
    close();
  };
  socket.onmessage = ({ data }) => {
    if (stopped) return;
    let value;
    if (text) {
      if (typeof data !== 'string' || !data.isWellFormed() || new TextEncoder().encode(data).length > 36_864) return close();
      value = data;
    } else {
      if (!(data instanceof ArrayBuffer) || data.byteLength < 1 || data.byteLength > 36_864) return close();
      value = new Uint8Array(data);
    }
    if (pending) { const waiter = pending; pending = null; waiter.resolve(value); }
    else if (queue.length < 4) queue.push(value);
    else close();
  };
  signal?.addEventListener('abort', close, { once: true });
  if (signal?.aborted) close();
  await opened;
  return Object.freeze({
    get closed() { return stopped; },
    get closeReason() { return closeReason; },
    get closeCode() { return closeCode; },
    close,
    onClose(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    async send(value, deadline = performance.now() + 10_000) {
      while (!stopped && socket.bufferedAmount > MAX_BUFFERED) {
        if (performance.now() >= deadline) { close(); throw refused(); }
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      if (stopped || signal?.aborted || socket.readyState !== 1 || performance.now() >= deadline) throw refused();
      socket.send(value);
    },
    async receive(timeoutMs = 10_000) {
      if (stopped || pending) throw refused();
      if (queue.length) return queue.shift();
      let timer;
      try {
        return await new Promise((resolve, reject) => {
          pending = { resolve, reject };
          timer = setTimeout(() => { close(); reject(refused()); }, timeoutMs);
        });
      } finally { clearTimeout(timer); }
    },
  });
}

export async function openCompanionChannel({ url, store, SnowSession, accountId, requestId, trust, signal, webSocketFactory, clock }) {
  const wire = await openLoopbackSocket(url, { signal, webSocketFactory });
  let session;
  try {
    session = await createCompanionSession({ store, SnowSession, accountId, requestId, trust, signal, clock, onClose: () => wire.close() });
    wire.onClose(() => session.close());
    const first = session.writeHandshake();
    const selected = new Uint8Array(first.length + 32);
    selected.set(key32(session.pairingId)); selected.set(first, 32);
    await wire.send(selected);
    session.readHandshake(await wire.receive(2_000));
    await wire.send(session.writeConfirmation());
    session.readConfirmation(await wire.receive(2_000));
    const identity = await session.authorize(await wire.receive(2_000));
    const pending = new Map();
    const abandoned = new Map();
    let rotationId = null;
    const discardAbandoned = (id) => {
      clearTimeout(abandoned.get(id)?.timer);
      abandoned.delete(id);
      if (rotationId === id) rotationId = null;
    };
    const observers = new Set();
    const controlObservers = new Set();
    const closedObservers = new Set();
    const fragments = createFragmentReceiver();
    let sending = Promise.resolve(), queued = 0, rotationQueued = 0, stopped = false;
    let closeDiagnostic = null;
    const diagnostic = cause => safeCompanionChannelDiagnostic({ cause,
      pendingRpcs: pending.size, abandonedRpcs: abandoned.size, queuedSends: queued + rotationQueued });
    const failure = cause => Object.assign(refused(), { diagnostic: diagnostic(cause) });
    function close(cause = wire.closed ? 'wire_closed' : 'local_close') {
      if (stopped) return;
      closeDiagnostic = diagnostic(cause);
      stopped = true;
      fragments.clear();
      session.close(); wire.close();
      for (const item of pending.values()) item.reject(Object.assign(refused(), { diagnostic: closeDiagnostic }));
      pending.clear();
      for (const id of abandoned.keys()) discardAbandoned(id);
      for (const listener of closedObservers) listener();
    }
    wire.onClose(close);
    const send = (document, rotation = false) => {
      if (stopped || (rotation ? rotationQueued >= 1 : queued >= 8)) {
        close('send_capacity');
        return Promise.reject(Object.assign(refused(), { diagnostic: closeDiagnostic }));
      }
      if (rotation) rotationQueued++;
      else queued++;
      const deadline = performance.now() + 10_000;
      const operation = sending.then(async () => {
        for (const frame of fragmentMessage(document)) {
          if (stopped || signal?.aborted) throw refused();
          await wire.send(session.seal(frame), deadline);
        }
      });
      sending = operation.catch(() => { close('send_failed'); });
      return operation.finally(() => { if (rotation) rotationQueued--; else queued--; });
    };
    async function rpc(method, params = {}, controls = {}) {
      const rotation = method === 'agent.storage.rotate';
      const ordinaryPending = pending.size - Number(pending.has(rotationId));
      const ordinaryTotal = ordinaryPending + abandoned.size - Number(abandoned.has(rotationId));
      const atCapacity = rotation ? rotationId !== null : ordinaryPending >= 8;
      const atBacklog = !rotation && ordinaryTotal >= 64;
      if (stopped || atCapacity || atBacklog || controls.signal?.aborted) {
        throw failure(stopped ? 'closed' : atCapacity ? 'rpc_capacity'
          : atBacklog ? 'rpc_backlog' : 'caller_aborted');
      }
      controls.assertCurrent?.();
      const id = crypto.randomUUID();
      if (rotation) rotationId = id;
      const deadline = performance.now() + 10_000;
      let timer, abort;
      const result = new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        abort = () => {
          if (!pending.delete(id)) return;
          abandoned.set(id, { timer, deadline });
          reject(failure('caller_aborted'));
        };
        timer = setTimeout(() => {
          if (abandoned.has(id)) { discardAbandoned(id); return; }
          close('rpc_timeout'); reject(failure('rpc_timeout'));
        }, 10_000);
        controls.signal?.addEventListener('abort', abort, { once: true });
      });
      void result.catch(() => undefined);
      try {
        const [, value] = await Promise.all([send({ type: 'rpc.request', id, method, params }, rotation), result]);
        controls.signal?.throwIfAborted(); controls.assertCurrent?.();
        return value;
      } finally {
        if (!abandoned.has(id)) clearTimeout(timer);
        controls.signal?.removeEventListener('abort', abort); pending.delete(id);
        if (rotationId === id && !abandoned.has(id)) rotationId = null;
      }
    }
    void (async () => {
      try {
        while (!stopped) {
          const document = fragments.receive(session.open(await wire.receive(fragments.remainingMs ?? 900_000)));
          if (document === null) continue;
          if (document.type === 'rpc.response') {
            const keys = Object.keys(document);
            const success = keys.length === 3 && keys.includes('result');
            const failure = keys.length === 3 && keys.includes('error') && typeof document.error === 'string' && /^[a-z_]{1,64}$/u.test(document.error);
            if (!keys.includes('id') || (!success && !failure)) throw refused();
            if (abandoned.has(document.id)) {
              const expired = performance.now() >= abandoned.get(document.id).deadline;
              discardAbandoned(document.id);
              if (expired) throw refused();
              continue;
            }
            if (!pending.has(document.id)) throw refused();
            const item = pending.get(document.id); pending.delete(document.id);
            if (failure) item.reject(new CompanionChannelError(document.error)); else item.resolve(document.result);
          } else if (document.type === 'session.control') {
            // A session-level control from Brain (ADR 0045). It is closed and
            // carries no data; the receiver applies it through its own controllers.
            const keys = Object.keys(document);
            if (keys.length !== 3 || typeof document.id !== 'string'
              || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(document.id)
              || !SESSION_CONTROLS.has(document.action)) throw refused();
            for (const listener of controlObservers) listener({ id: document.id, action: document.action });
          } else {
            if (document.protocol_version !== '2' || observers.size !== 1) throw refused();
            for (const listener of observers) listener(document);
          }
        }
      } catch { close('receive_failed'); }
    })();
    return Object.freeze({
      identity: Object.freeze({ ...identity, pairing_id: session.pairingId }),
      get closed() { return stopped; },
      get closeReason() { return wire.closeReason; },
      get closeCode() { return wire.closeCode; },
      get closeDiagnostic() { return safeCompanionChannelDiagnostic(closeDiagnostic); },
      rpc, send: document => send(document), close,
      onMessage(listener) { if (observers.size) throw refused(); observers.add(listener); return () => observers.delete(listener); },
      onControl(listener) { controlObservers.add(listener); return () => controlObservers.delete(listener); },
      onClose(listener) { closedObservers.add(listener); return () => closedObservers.delete(listener); },
    });
  } catch {
    session?.close(); wire.close(); throw refused();
  }
}
