import init, { SnowSession } from './pkg/ofca_snow_wasm_spike.js';
import { sessionPrologue, fromHex, toHex } from './noise-binding.mjs';
import {
  PAIRING_DIGEST_HEX,
  AGENT_PRIVATE_HEX,
  BRAIN_PUBLIC_HEX,
} from './fixture.mjs';

chrome.runtime.onInstalled.addListener(() => {});
chrome.runtime.onStartup.addListener(() => {});

const ready = init();
const ORDINARY_LIMIT = 4079;
const AUTHORIZATION_LIMIT = 36847;
const CLIENT_READY = new TextEncoder().encode('client-ready');
const SERVER_READY = new TextEncoder().encode('server-ready');
const APP = 1;
const CONTROL = 0;
let replayCapture = null;

function bytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  throw new Error('invalid_bytes');
}
function record(kind, payload) {
  const value = bytes(payload);
  const out = new Uint8Array(value.length + 1);
  out[0] = kind;
  out.set(value, 1);
  return out;
}
function equal(a, b) { return a.length === b.length && a.every((v, i) => v === b[i]); }
function sealOrdinary(session, payload) {
  const value = bytes(payload);
  if (value.length > ORDINARY_LIMIT) throw new Error('ordinary_record_too_large');
  return bytes(session.encrypt_transport(record(APP, value)));
}
function flip(value) { const out = new Uint8Array(value); out[0] ^= 1; return out; }
function message(ws, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new Error('deadline')); }, timeout);
    const cleanup = () => {
      clearTimeout(timer);
      ws.removeEventListener('message', onMessage);
      ws.removeEventListener('close', onClose);
      ws.removeEventListener('error', onError);
    };
    const onMessage = event => { cleanup(); resolve(bytes(event.data)); };
    const onClose = () => { cleanup(); reject(new Error('closed')); };
    const onError = () => { cleanup(); reject(new Error('transport')); };
    ws.addEventListener('message', onMessage, {once: true});
    ws.addEventListener('close', onClose, {once: true});
    ws.addEventListener('error', onError, {once: true});
  });
}
function opened(ws, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new Error('deadline')); }, timeout);
    const cleanup = () => {
      clearTimeout(timer);
      ws.removeEventListener('open', onOpen);
      ws.removeEventListener('close', onClose);
      ws.removeEventListener('error', onError);
    };
    const onOpen = () => { cleanup(); resolve(); };
    const onClose = () => { cleanup(); reject(new Error('closed')); };
    const onError = () => { cleanup(); reject(new Error('transport')); };
    ws.addEventListener('open', onOpen, {once: true});
    ws.addEventListener('close', onClose, {once: true});
    ws.addEventListener('error', onError, {once: true});
  });
}
async function connect(caseName) {
  const ws = new WebSocket(`ws://127.0.0.1:17871/native-snow-spike?case=${encodeURIComponent(caseName)}`);
  ws.binaryType = 'arraybuffer';
  await opened(ws);
  return ws;
}
async function newSession() {
  await ready;
  return new SnowSession(
    true,
    fromHex(AGENT_PRIVATE_HEX),
    fromHex(BRAIN_PUBLIC_HEX),
    sessionPrologue(fromHex(PAIRING_DIGEST_HEX)),
  );
}
async function handshakeAndConfirm(caseName) {
  const ws = await connect(caseName);
  const session = await newSession();
  try {
    const first = bytes(session.write_handshake());
    ws.send(first);
    const second = await message(ws);
    session.read_handshake(second);
    if (!session.handshake_finished()) throw new Error('handshake_incomplete');
    const handshakeHash = toHex(bytes(session.handshake_hash()));
    session.enter_transport();
    const clientReady = bytes(session.encrypt_transport(record(CONTROL, CLIENT_READY)));
    ws.send(clientReady);
    const serverReady = bytes(session.decrypt_transport(await message(ws)));
    if (serverReady[0] !== CONTROL || !equal(serverReady.slice(1), SERVER_READY)) throw new Error('server_confirmation');
    return {ws, session, first, second, clientReady, handshakeHash};
  } catch (error) {
    try { session.close(); } catch {}
    try { session.free(); } catch {}
    try { ws.close(); } catch {}
    throw error;
  }
}
async function authorize(state) {
  const plain = bytes(state.session.decrypt_transport(await message(state.ws)));
  if (plain.length !== AUTHORIZATION_LIMIT + 1 || plain[0] !== APP) throw new Error('authorization_size');
  const text = new TextDecoder().decode(plain.slice(1)).trimEnd();
  const parsed = JSON.parse(text);
  if (parsed.type !== 'session.authorization') throw new Error('authorization_type');
  return plain;
}
async function fullPositive(caseName) {
  const state = await handshakeAndConfirm(caseName);
  try {
    await authorize(state);
    const payload = new Uint8Array(ORDINARY_LIMIT).fill(65);
    state.ws.send(sealOrdinary(state.session, payload));
    const reply = bytes(state.session.decrypt_transport(await message(state.ws)));
    if (reply.length !== ORDINARY_LIMIT + 1 || reply[0] !== APP) throw new Error('ordinary_reply_size');
    return {
      result: 'passed',
      first: toHex(state.first),
      second: toHex(state.second),
      clientReady: toHex(state.clientReady),
      handshakeHash: state.handshakeHash,
    };
  } finally {
    try { state.ws.close(); } catch {}
    try { state.session.close(); } catch {}
    try { state.session.free(); } catch {}
  }
}
async function expectRefusal(name, fn) {
  try { await fn(); }
  catch { return {result: 'passed'}; }
  throw new Error(`${name}_accepted`);
}

async function runAll() {
  const results = {};
  results.positive = await fullPositive('positive');
  results['fresh-1'] = await fullPositive('fresh-1');
  results['fresh-2'] = await fullPositive('fresh-2');
  if (results['fresh-1'].first === results['fresh-2'].first) throw new Error('agent_ephemeral_reused');

  for (const name of ['wrong-brain-key', 'wrong-agent-key', 'wrong-prologue', 'altered-digest']) {
    results[name] = await expectRefusal(name, async () => {
      const state = await handshakeAndConfirm(name);
      try { await authorize(state); } finally { state.session.close(); state.ws.close(); }
    });
  }

  results['tampered-handshake'] = await expectRefusal('tampered-handshake', async () => {
    const ws = await connect('tampered-handshake');
    const session = await newSession();
    try { ws.send(flip(bytes(session.write_handshake()))); await message(ws); }
    finally { session.close(); ws.close(); }
  });

  replayCapture = await fullPositive('replay-capture');
  results['replay-capture'] = {result: 'passed', handshakeHash: replayCapture.handshakeHash};
  results.replay = await expectRefusal('replay', async () => {
    const ws = await connect('replay');
    try {
      ws.send(fromHex(replayCapture.first));
      await message(ws);
      ws.send(fromHex(replayCapture.clientReady));
      await message(ws);
    } finally { ws.close(); }
  });
  results.reordered = await expectRefusal('reordered', async () => {
    const ws = await connect('reordered');
    try { ws.send(fromHex(replayCapture.clientReady)); await message(ws); } finally { ws.close(); }
  });

  results['tampered-transport'] = await expectRefusal('tampered-transport', async () => {
    const state = await handshakeAndConfirm('tampered-transport');
    try {
      await authorize(state);
      const valid = sealOrdinary(state.session, new Uint8Array([1,2,3]));
      state.ws.send(flip(valid));
      await message(state.ws);
    } finally { state.session.close(); state.ws.close(); }
  });

  results['replayed-transport'] = await expectRefusal('replayed-transport', async () => {
    const state = await handshakeAndConfirm('replayed-transport');
    try {
      await authorize(state);
      const frame = sealOrdinary(state.session, new Uint8Array([4,5,6]));
      state.ws.send(frame);
      const ack = bytes(state.session.decrypt_transport(await message(state.ws)));
      if (ack[0] !== APP) throw new Error('missing_ack');
      state.ws.send(frame);
      await message(state.ws);
    } finally { state.session.close(); state.ws.close(); }
  });

  results['oversize-ordinary'] = await expectRefusal('oversize-ordinary', async () => {
    const state = await handshakeAndConfirm('oversize-ordinary');
    try {
      await authorize(state);
      sealOrdinary(state.session, new Uint8Array(ORDINARY_LIMIT + 1));
    } finally { state.session.close(); state.ws.close(); }
  });

  results['oversize-authorization'] = await expectRefusal('oversize-authorization', async () => {
    const state = await handshakeAndConfirm('oversize-authorization');
    try { await message(state.ws); } finally { state.session.close(); state.ws.close(); }
  });

  results['malformed-frame'] = await expectRefusal('malformed-frame', async () => {
    const ws = await connect('malformed-frame');
    try { ws.send('malformed'); await message(ws); } finally { ws.close(); }
  });

  results.cancel = await expectRefusal('cancel', async () => {
    const ws = await connect('cancel');
    const session = await newSession();
    ws.send(bytes(session.write_handshake()));
    session.close();
    ws.close();
    await new Promise(resolve => setTimeout(resolve, 100));
    throw new Error('cancelled_locally');
  });

  results.deadline = await expectRefusal('deadline', async () => {
    const ws = await connect('deadline');
    const session = await newSession();
    try {
      await new Promise(resolve => setTimeout(resolve, 200));
      ws.send(bytes(session.write_handshake()));
      await message(ws);
    } finally { session.close(); ws.close(); }
  });

  await ready;
  const closed = await newSession();
  closed.close();
  let useAfterClose = false;
  try { closed.write_handshake(); } catch { useAfterClose = true; }
  try { closed.free(); } catch {}
  if (!useAfterClose) throw new Error('wasm_use_after_close_accepted');
  results['use-after-close'] = {result: 'passed'};

  return results;
}

globalThis.__nativeSnowRun = runAll;
