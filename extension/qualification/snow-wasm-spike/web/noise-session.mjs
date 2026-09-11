const MAX_FRAME = 36864;
const MAX_APP_PLAINTEXT = 4096 - 17;
const CONTROL = 0;
const APPLICATION = 1;
const CLIENT_READY = new TextEncoder().encode('client-ready');
const SERVER_READY = new TextEncoder().encode('server-ready');

export class SessionFailure extends Error {
  constructor(code) { super(code); this.name = 'SessionFailure'; this.code = code; }
}
function bytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  throw new SessionFailure('invalid_frame');
}
function equal(a, b) { return a.length === b.length && a.every((value, index) => value === b[index]); }
function record(kind, payload) { const out = new Uint8Array(1 + payload.length); out[0] = kind; out.set(payload, 1); return out; }

export class NoiseSession {
  #inner; #initiator; #state = 'handshake'; #sentConfirmation = false; #readConfirmation = false;
  #timer = null; #signal; #abortHandler;
  constructor({ SnowSession, initiator, localPrivateKey, remotePublicKey, prologue, signal, timeoutMs = 2000 }) {
    if (signal?.aborted) throw new SessionFailure('cancelled');
    this.#initiator = Boolean(initiator); this.#signal = signal;
    try { this.#inner = new SnowSession(this.#initiator, bytes(localPrivateKey), bytes(remotePublicKey), bytes(prologue)); }
    catch { throw new SessionFailure('handshake_init_failed'); }
    if (signal) { this.#abortHandler = () => this.close('cancelled'); signal.addEventListener('abort', this.#abortHandler, {once: true}); }
    if (Number.isFinite(timeoutMs) && timeoutMs > 0) this.#timer = setTimeout(() => this.close('deadline_exceeded'), timeoutMs);
  }
  get state() { return this.#state; }
  #require(state) { if (this.#state !== state) throw new SessionFailure(this.#state === 'closed' ? 'session_closed' : 'unexpected_state'); }
  #fail(code) { this.close(code); throw new SessionFailure(code); }
  #guard(action, code) { try { return action(); } catch { return this.#fail(code); } }
  writeHandshake() { this.#require('handshake'); return bytes(this.#guard(() => this.#inner.write_handshake(), 'handshake_failed')); }
  readHandshake(frame) { this.#require('handshake'); const value = bytes(frame); if (!value.length || value.length > MAX_FRAME) this.#fail('invalid_frame'); this.#guard(() => this.#inner.read_handshake(value), 'handshake_failed'); }
  finishHandshake() { this.#require('handshake'); if (!this.#inner.handshake_finished()) this.#fail('handshake_incomplete'); this.#guard(() => this.#inner.enter_transport(), 'handshake_failed'); this.#state = 'confirming'; }
  writeConfirmation() {
    this.#require('confirming');
    if (this.#sentConfirmation || (!this.#initiator && !this.#readConfirmation)) this.#fail('unexpected_record');
    const expected = this.#initiator ? CLIENT_READY : SERVER_READY;
    this.#sentConfirmation = true;
    const out = bytes(this.#guard(() => this.#inner.encrypt_transport(record(CONTROL, expected)), 'encryption_failed'));
    if (!this.#initiator) this.#state = 'ready';
    return out;
  }
  readConfirmation(frame) {
    this.#require('confirming');
    if (this.#readConfirmation || (this.#initiator && !this.#sentConfirmation)) this.#fail('unexpected_record');
    const plain = bytes(this.#guard(() => this.#inner.decrypt_transport(bytes(frame)), 'authentication_failed'));
    const expected = this.#initiator ? SERVER_READY : CLIENT_READY;
    if (plain[0] !== CONTROL || !equal(plain.slice(1), expected)) this.#fail('confirmation_failed');
    this.#readConfirmation = true;
    if (this.#initiator) this.#state = 'ready';
  }
  seal(payload) { this.#require('ready'); const value = bytes(payload); if (value.length > MAX_APP_PLAINTEXT) this.#fail('payload_too_large'); return bytes(this.#guard(() => this.#inner.encrypt_transport(record(APPLICATION, value)), 'encryption_failed')); }
  open(frame) { this.#require('ready'); const value = bytes(frame); if (!value.length || value.length > MAX_FRAME) this.#fail('invalid_frame'); const plain = bytes(this.#guard(() => this.#inner.decrypt_transport(value), 'authentication_failed')); if (plain[0] !== APPLICATION) this.#fail('unexpected_record'); return plain.slice(1); }
  close(reason = 'session_closed') {
    if (this.#state === 'closed') return;
    this.#state = 'closed';
    if (this.#timer) clearTimeout(this.#timer);
    if (this.#signal && this.#abortHandler) this.#signal.removeEventListener('abort', this.#abortHandler);
    try { this.#inner?.close(); } catch {}
    try { this.#inner?.free(); } catch {}
    this.#inner = null; this.closeReason = reason;
  }
}

function once(ws, kind, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new SessionFailure('deadline_exceeded')); }, timeoutMs);
    const cleanup = () => { clearTimeout(timer); ws.removeEventListener(kind, ok); ws.removeEventListener('error', fail); ws.removeEventListener('close', fail); };
    const ok = event => { cleanup(); resolve(event); };
    const fail = () => { cleanup(); reject(new SessionFailure('transport_failed')); };
    ws.addEventListener(kind, ok, {once: true}); ws.addEventListener('error', fail, {once: true}); ws.addEventListener('close', fail, {once: true});
  });
}
export async function connectInitiator({ WebSocketImpl = WebSocket, url, session, timeoutMs = 2000 }) {
  const ws = new WebSocketImpl(url); ws.binaryType = 'arraybuffer';
  try {
    await once(ws, 'open', timeoutMs);
    ws.send(session.writeHandshake());
    session.readHandshake(bytes((await once(ws, 'message', timeoutMs)).data));
    session.finishHandshake();
    ws.send(session.writeConfirmation());
    session.readConfirmation(bytes((await once(ws, 'message', timeoutMs)).data));
    return ws;
  } catch (error) {
    session.close(error?.code ?? 'transport_failed');
    try { ws.close(); } catch {}
    throw error instanceof SessionFailure ? error : new SessionFailure('transport_failed');
  }
}
