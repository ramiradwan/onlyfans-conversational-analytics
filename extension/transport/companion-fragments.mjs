import { b64u, unb64u } from './pairing-contract.mjs';
import { parseStrictJson } from './grant-verifier.mjs';

export const MAX_COMPANION_MESSAGE_BYTES = 524_288;
export const MAX_FRAGMENT_BYTES = 2_800;
export const FRAGMENT_DEADLINE_MS = 10_000;
const MAX_RECORD_BYTES = 4_079;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const fail = () => { throw Object.assign(new Error('session_fragment_refused'), { code: 'session_fragment_refused' }); };

export function fragmentMessage(document, id = crypto.randomUUID()) {
  if (!UUID.test(id)) fail();
  let bytes;
  try { bytes = encoder.encode(JSON.stringify(document)); } catch { fail(); }
  if (bytes.length < 1 || bytes.length > MAX_COMPANION_MESSAGE_BYTES) fail();
  const fragments = [];
  for (let offset = 0, index = 0; offset < bytes.length; offset += MAX_FRAGMENT_BYTES, index++) {
    const chunk = bytes.subarray(offset, offset + MAX_FRAGMENT_BYTES);
    const record = encoder.encode(JSON.stringify({
      type: 'fragment', id, index, final: offset + chunk.length === bytes.length, data: b64u(chunk),
    }));
    if (record.length > MAX_RECORD_BYTES) fail();
    fragments.push(record);
  }
  return fragments;
}

export function createFragmentReceiver({ now = () => performance.now() } = {}) {
  let current = null;
  return Object.freeze({
    clear() { current = null; },
    get remainingMs() {
      if (current === null) return null;
      const elapsed = now() - current.started;
      if (!Number.isFinite(elapsed) || elapsed < 0) fail();
      return Math.max(0, FRAGMENT_DEADLINE_MS - elapsed);
    },
    receive(bytes) {
      try {
        if (!(bytes instanceof Uint8Array) || bytes.length < 1 || bytes.length > MAX_RECORD_BYTES) fail();
        const record = parseStrictJson(bytes);
        if (!record || Array.isArray(record) || Object.keys(record).length !== 5
          || !['type', 'id', 'index', 'final', 'data'].every((key) => Object.hasOwn(record, key))) fail();
        if (record.type !== 'fragment' || !UUID.test(record.id)
          || !Number.isSafeInteger(record.index) || record.index < 0 || typeof record.final !== 'boolean') fail();
        const chunk = unb64u(record.data);
        if (chunk.length < 1 || chunk.length > MAX_FRAGMENT_BYTES) fail();
        if (!record.final && chunk.length !== MAX_FRAGMENT_BYTES) fail();
        const time = now();
        if (!Number.isFinite(time)) fail();
        if (current === null) {
          if (record.index !== 0) fail();
          current = { id: record.id, index: 0, started: time, chunks: [], length: 0 };
        }
        if (current.id !== record.id || current.index !== record.index
          || time < current.started || time - current.started >= FRAGMENT_DEADLINE_MS
          || current.length + chunk.length > MAX_COMPANION_MESSAGE_BYTES) fail();
        current.index++;
        current.length += chunk.length;
        current.chunks.push(chunk);
        if (!record.final) return null;
        const message = new Uint8Array(current.length);
        let offset = 0;
        for (const part of current.chunks) { message.set(part, offset); offset += part.length; }
        current = null;
        parseStrictJson(message); // Reject duplicates before normal protocol numeric decoding.
        const document = JSON.parse(decoder.decode(message));
        if (!document || typeof document !== 'object' || Array.isArray(document)) fail();
        const check = (value, depth = 0) => {
          if (depth > 64) fail();
          if (typeof value === 'number' && !Number.isFinite(value)) fail();
          if (typeof value === 'string' && !value.isWellFormed()) fail();
          if (value && typeof value === 'object') {
            for (const [key, entry] of Object.entries(value)) {
              if (!key.isWellFormed()) fail();
              check(entry, depth + 1);
            }
          }
        };
        check(document);
        return document;
      } catch {
        current = null;
        fail();
      }
    },
  });
}
