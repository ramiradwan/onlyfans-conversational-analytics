import { createCompanionSession } from './companion-noise-session.mjs';
import { key32 } from './pairing-contract.mjs';
import { createFragmentReceiver, fragmentMessage } from './companion-fragments.mjs';

export class CompanionChannelError extends Error {
  constructor(code = 'companion_session_refused') { super(code); this.code = code; }
}
const refused = () => new CompanionChannelError();
const MAX_BUFFERED = 128 * 1024;

export async function openLoopbackSocket(url, { webSocketFactory = (value) => new WebSocket(value), signal, text = false } = {}) {
  const socket = webSocketFactory(url);
  socket.binaryType = 'arraybuffer';
  let pending = null, stopped = false, closeReason = null;
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
    if (!stopped && typeof event?.reason === 'string' && /^[a-z_]{1,64}$/u.test(event.reason)) closeReason = event.reason;
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
    const observers = new Set();
    const closedObservers = new Set();
    const fragments = createFragmentReceiver();
    let sending = Promise.resolve(), queued = 0, stopped = false;
    function close() {
      if (stopped) return;
      stopped = true;
      fragments.clear();
      session.close(); wire.close();
      for (const item of pending.values()) item.reject(refused());
      pending.clear();
      for (const listener of closedObservers) listener();
    }
    wire.onClose(close);
    const send = (document) => {
      if (stopped || ++queued > 8) { queued--; close(); return Promise.reject(refused()); }
      const deadline = performance.now() + 10_000;
      const operation = sending.then(async () => {
        for (const frame of fragmentMessage(document)) {
          if (stopped || signal?.aborted) throw refused();
          await wire.send(session.seal(frame), deadline);
        }
      });
      sending = operation.catch(() => { close(); });
      return operation.finally(() => queued--);
    };
    async function rpc(method, params = {}, controls = {}) {
      if (stopped || pending.size >= 8 || controls.signal?.aborted) throw refused();
      controls.assertCurrent?.();
      const id = crypto.randomUUID();
      let timer, abort;
      const result = new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        abort = () => { pending.delete(id); close(); reject(refused()); };
        timer = setTimeout(abort, 10_000);
        controls.signal?.addEventListener('abort', abort, { once: true });
      });
      void result.catch(() => undefined);
      try {
        await send({ type: 'rpc.request', id, method, params });
        const value = await result;
        controls.signal?.throwIfAborted(); controls.assertCurrent?.();
        return value;
      } finally {
        clearTimeout(timer); controls.signal?.removeEventListener('abort', abort); pending.delete(id);
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
            if (!keys.includes('id') || (!success && !failure) || !pending.has(document.id)) throw refused();
            const item = pending.get(document.id); pending.delete(document.id);
            if (failure) item.reject(new CompanionChannelError(document.error)); else item.resolve(document.result);
          } else {
            if (document.protocol_version !== '2' || observers.size !== 1) throw refused();
            for (const listener of observers) listener(document);
          }
        }
      } catch { close(); }
    })();
    return Object.freeze({
      identity: Object.freeze({ ...identity, pairing_id: session.pairingId }),
      get closed() { return stopped; },
      rpc, send, close,
      onMessage(listener) { if (observers.size) throw refused(); observers.add(listener); return () => observers.delete(listener); },
      onClose(listener) { closedObservers.add(listener); return () => closedObservers.delete(listener); },
    });
  } catch {
    session?.close(); wire.close(); throw refused();
  }
}
