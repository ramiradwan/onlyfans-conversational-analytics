// Isolated public conformance keys (scalars 1 and 2). Never production material.
import vectors from '../node_modules/local-authenticated-read-connector/publication/vectors.json' with { type: 'json' };

export const syntheticTrust = vectors.trust;
export const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
  : value && typeof value === 'object' ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
    : JSON.stringify(value);
export const b64 = bytes => Buffer.from(bytes).toString('base64url');
export async function digest(bytes) { return b64(await crypto.subtle.digest('SHA-256', bytes)); }

async function signed(payload, jwk, scalar, kid, typ) {
  const privateKey = await crypto.subtle.importKey('jwk', { ...jwk, d: b64(Uint8Array.from({ length: 32 }, (_, i) => i === 31 ? scalar : 0)) },
    { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const text = b64(Buffer.from(canonical({ alg: 'ES256', kid, typ }))) + '.' + b64(Buffer.from(canonical(payload)));
  return text + '.' + b64(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, privateKey, Buffer.from(text)));
}

export async function publications(rule, { now, sequence = 1, expiresAt = now + 86400, empty = false, engine = 'onlyfans-sha1-checksum/v1' } = {}) {
  const original = JSON.parse(Buffer.from(vectors.fixtures[0].keyset.split('.')[1], 'base64url'));
  const keys = { ...original, issued_at: now - 3600, expires_at: now + 30 * 86400,
    keys: original.keys.map(key => ({ ...key, not_before: now - 3600, not_after: now + 30 * 86400 })) };
  const object = Buffer.from(JSON.stringify(rule, null, 2) + '\n');
  const hash = Buffer.from(await crypto.subtle.digest('SHA-256', object)).toString('hex');
  const payload = { schema: 'signing-parameter-catalog/v1', sequence, issued_at: now - 1, expires_at: expiresAt,
    platform: 'onlyfans', engine, parameter_schema: 'local-packaged-signing-rule/v1',
    rules: empty ? [] : [{ source_revision: rule.source_revision, sha256: hash, size: object.length }] };
  return { keyset: await signed(keys, syntheticTrust.roots[0].jwk, 1, 'synthetic-root', 'signing-publisher-keyset+jws'),
    catalog: await signed(payload, keys.keys[0].jwk, 2, 'synthetic-publisher', 'signing-parameter-catalog+jws'), object: b64(object), hash };
}
