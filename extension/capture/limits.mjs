export const CAPTURE_LIMITS = Object.freeze({
  responseBytes: 2 * 1024 * 1024,
  textBytes: 128 * 1024,
  displayNameBytes: 1024,
  envelopeBytes: 192 * 1024,
  queueBytes: 4 * 1024 * 1024,
  queueEntries: 128,
  retryWindowMs: 120_000,
});

const encoder = new TextEncoder();

export function utf8Bytes(value) {
  return encoder.encode(value).byteLength;
}

export function fitsUtf8(value, maxBytes) {
  return typeof value === 'string'
    && value.length <= maxBytes
    && utf8Bytes(value) <= maxBytes;
}
