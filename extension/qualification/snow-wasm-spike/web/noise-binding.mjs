const encoder = new TextEncoder();
export const SESSION_PROFILE = 'ofca-companion-session/v1;agent-to-brain;no-early-data';

export function fromHex(hex) {
  if (typeof hex !== 'string' || hex.length % 2 !== 0 || !/^[0-9a-f]*$/u.test(hex)) throw new TypeError('invalid_hex');
  return Uint8Array.from(hex.match(/../gu) ?? [], value => Number.parseInt(value, 16));
}
export function toHex(bytes) { return [...bytes].map(value => value.toString(16).padStart(2, '0')).join(''); }

// The session profile, one zero byte, then the pin's 32-byte pairing digest.
export function sessionPrologue(pairingDigest) {
  if (!(pairingDigest instanceof Uint8Array) || pairingDigest.length !== 32) throw new TypeError('invalid_pairing_digest');
  const prefix = encoder.encode(SESSION_PROFILE);
  const out = new Uint8Array(prefix.length + 33);
  out.set(prefix);
  out.set(pairingDigest, prefix.length + 1);
  return out;
}
