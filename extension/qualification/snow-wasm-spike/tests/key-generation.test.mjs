import test from 'node:test';
import assert from 'node:assert/strict';
import { createPrivateKey, createPublicKey } from 'node:crypto';
import { loadWasm } from './helpers.mjs';
import { snowKeypairGenerator } from '../../../runtime/companion-agent-identity.mjs';

test('Snow CSPRNG generates independent static keys with matching X25519 public keys', async () => {
  await loadWasm();
  const wasm = await import('../pkg/ofca_snow_wasm_spike.js');
  const generate = snowKeypairGenerator(wasm.generate_static_keypair);
  const first = generate(), second = generate();
  try {
    assert.notDeepEqual(first.privateKey, second.privateKey);
    for (const pair of [first, second]) {
      // Independent Node/OpenSSL derivation from the generated private scalar.
      const der = Buffer.concat([Buffer.from('302e020100300506032b656e04220420', 'hex'), pair.privateKey]);
      const privateKey = createPrivateKey({key: der, format: 'der', type: 'pkcs8'});
      const jwk = createPublicKey(privateKey).export({format: 'jwk'});
      assert.deepEqual(pair.publicKey, new Uint8Array(Buffer.from(jwk.x, 'base64url')));
      der.fill(0);
    }
  } finally { first.privateKey.fill(0); second.privateKey.fill(0); }
});
