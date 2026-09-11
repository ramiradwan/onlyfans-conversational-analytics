import { fitsUtf8 } from './limits.mjs';

export function parseBoundedJson(text, maxBytes) {
  if (!fitsUtf8(text, maxBytes)) throw new Error('capture_response_too_large');
  return JSON.parse(text);
}

export async function readBoundedJson(response, maxBytes, timeoutMs = 5_000) {
  const length = Number(response.headers?.get('content-length'));
  if (Number.isFinite(length) && length > maxBytes) {
    void response.body?.cancel().catch(() => undefined);
    throw new Error('capture_response_too_large');
  }
  if (!response.body?.getReader) throw new Error('capture_response_unreadable');
  const reader = response.body.getReader();
  let timer;
  let completed = false;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('capture_response_timeout')), timeoutMs);
  });
  const decoder = new TextDecoder();
  let bytes = 0;
  let text = '';
  try {
    while (true) {
      const part = await Promise.race([reader.read(), timeout]);
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > maxBytes) throw new Error('capture_response_too_large');
      text += decoder.decode(part.value, { stream: true });
    }
    completed = true;
    text += decoder.decode();
    return JSON.parse(text);
  } finally {
    clearTimeout(timer);
    if (!completed) void reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
