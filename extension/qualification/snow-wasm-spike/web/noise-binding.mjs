const encoder = new TextEncoder();
const DOMAIN = encoder.encode('OFCA-COMPANION-PAIRING-BINDING-V1\0');
const TYPE = 'ofca-companion-pairing+jwt';

export function fromHex(hex) {
  if (typeof hex !== 'string' || hex.length % 2 !== 0 || !/^[0-9a-f]*$/u.test(hex)) throw new TypeError('invalid_hex');
  return Uint8Array.from(hex.match(/../gu) ?? [], value => Number.parseInt(value, 16));
}
export function toHex(bytes) { return [...bytes].map(value => value.toString(16).padStart(2, '0')).join(''); }
function fromBase64Url(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/u.test(value) || value.includes('=')) throw new TypeError('invalid_base64url');
  const base64 = value.replace(/-/gu, '+').replace(/_/gu, '/') + '='.repeat((4 - value.length % 4) % 4);
  const binary = atob(base64);
  const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
  let canonical = '';
  for (const byte of bytes) canonical += String.fromCharCode(byte);
  canonical = btoa(canonical).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/u, '');
  if (canonical !== value) throw new TypeError('invalid_base64url');
  return bytes;
}
function u64(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw new TypeError('invalid_integer');
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, BigInt(value), false);
  return out;
}
function fieldBytes(value) {
  if (typeof value === 'string') return encoder.encode(value);
  if (typeof value === 'number') return u64(value);
  if (value instanceof Uint8Array) return value;
  throw new TypeError('invalid_canonical_field');
}
function lengthPrefix(bytes) {
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length, false);
  out.set(bytes, 4);
  return out;
}
function concat(parts) {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) { out.set(part, offset); offset += part.length; }
  return out;
}
export async function receiptBinding(claims) {
  const raw32 = (hex) => { const value = fromHex(hex); if (value.length !== 32) throw new TypeError('invalid_raw32'); return value; };
  const key32 = (value) => { const bytes = fromBase64Url(value); if (bytes.length !== 32 || value.length !== 43) throw new TypeError('invalid_key'); return bytes; };
  const fields = [
    TYPE, claims.iss, claims.aud, claims.iat, claims.exp, claims.jti, claims.suite,
    claims.organization_id, claims.installation_id, claims.installation_key_id,
    claims.installation_key_jkt, claims.agent_id, claims.agent_identity_key_id,
    claims.agent_identity_key_jkt, claims.account_id, claims.pairing_id,
    key32(claims.agent_key), key32(claims.brain_key), raw32(claims.agent_nonce),
    raw32(claims.brain_nonce), claims.generation, raw32(claims.grant_digest),
    claims.approval_id, claims.approval_revision, claims.offline_not_after,
  ];
  const canonical = concat([DOMAIN, ...fields.map(value => lengthPrefix(fieldBytes(value)))]);
  return new Uint8Array(await crypto.subtle.digest('SHA-256', canonical));
}
export function spikePrologue(binding, prefix = 'ofca-session-spike/v1;agent-to-brain;no-early-data') {
  if (!(binding instanceof Uint8Array) || binding.length !== 32) throw new TypeError('invalid_binding');
  return concat([encoder.encode(prefix), Uint8Array.of(0), binding]);
}
