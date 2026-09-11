import test from 'node:test'; import assert from 'node:assert/strict'; import { readFile } from 'node:fs/promises';
import { sessionPrologue, fromHex, toHex } from '../web/noise-binding.mjs';
import { PAIRING_DIGEST_HEX } from '../web/fixture.mjs';
const vector = JSON.parse(await readFile(new URL('../../../test-fixtures/pairing/local-pairing-vector.json', import.meta.url), 'utf8'));
test('spike session prologue reproduces the local pairing vector', () => {
  assert.equal(PAIRING_DIGEST_HEX, vector.expected.pairing_digest);
  assert.equal(toHex(sessionPrologue(fromHex(PAIRING_DIGEST_HEX))), vector.expected.session_prologue);
  assert.throws(() => sessionPrologue(new Uint8Array(31)), TypeError);
});
