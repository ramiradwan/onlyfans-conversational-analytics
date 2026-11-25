import limits from '../../shared/desktop-onboarding/wire-limits.json' with { type: 'json' };
import { ONBOARDING_JSON_BYTES } from '../../shared/onboarding/json.mjs';

// A data line may include "data: " and a carriage return beyond the document.
const DATA_LINE_BYTES = limits.setup_frame_utf8_bytes + 7;
export const SCOPED_SETUP_EVENT = 'setup-frame.v1';
const OTHER_LINE_BYTES = 8192;

/** Header-authenticated, bounded stream. Every setup event contains the entire projection. */
export async function setupEvents(request, headers, signal, receive) {
  const response = await request('/api/v1/desktop/setup/events', { credentials: 'same-origin', redirect: 'error',
    cache: 'no-store', headers: { Accept: 'text/event-stream', 'X-Desktop-Setup-Events': SCOPED_SETUP_EVENT, ...headers }, signal });
  if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'text/event-stream') {
    throw new Error(response.status === 403 ? 'authorization_unavailable' : 'stream_unavailable');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  const encoder = new TextEncoder();
  const buffer = new Uint8Array(DATA_LINE_BYTES);
  let used = 0, data = '', dataBytes = 0, event = '', timeout;
  let firstLine = true;
  const receiveLine = (raw) => {
    let line = decoder.decode(raw).replace(/\r$/u, '');
    if (firstLine) {
      line = line.replace(/^\uFEFF/u, '');
      firstLine = false;
    }
    if (!line) {
      const budget = event === SCOPED_SETUP_EVENT ? limits.setup_frame_utf8_bytes
        : event === 'setup' ? limits.document_utf8_bytes : ONBOARDING_JSON_BYTES;
      if (dataBytes > budget) throw new Error('invalid_stream');
      if (event === 'setup' && data) receive(data);
      else if (event === SCOPED_SETUP_EVENT && data) receive(data, event);
      else if (event === 'setup-end' && data === '{}') throw new Error('stream_expired');
      data = ''; dataBytes = 0; event = '';
    } else if (line.startsWith('data:')) {
      const field = line.slice(5).replace(/^ /u, '');
      const separator = data ? '\n' : '';
      dataBytes += separator.length + encoder.encode(field).byteLength;
      if (dataBytes > limits.setup_frame_utf8_bytes) throw new Error('invalid_stream');
      data += separator + field;
    } else {
      if (raw.byteLength > OTHER_LINE_BYTES) throw new Error('invalid_stream');
      if (line.startsWith('event:')) event = line.slice(6).trim();
    }
  };
  const cancel = () => { void reader.cancel().catch(() => {}); };
  const arm = () => { clearTimeout(timeout); timeout = setTimeout(cancel, 45_000); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    arm();
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      arm();
      let offset = 0;
      while (offset < value.byteLength && !signal.aborted) {
        const newline = value.indexOf(10, offset);
        const end = newline < 0 ? value.byteLength : newline;
        const length = end - offset;
        if (used + length > buffer.byteLength) throw new Error('invalid_stream');
        buffer.set(value.subarray(offset, end), used);
        used += length;
        if (newline < 0) break;
        receiveLine(buffer.subarray(0, used));
        used = 0;
        offset = end + 1;
      }
    }
    if (!signal.aborted) throw new Error('stream_unavailable');
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
