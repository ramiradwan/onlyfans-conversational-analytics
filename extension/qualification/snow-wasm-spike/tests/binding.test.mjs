import test from 'node:test'; import assert from 'node:assert/strict';
import { sessionPrologue, fromHex, toHex } from '../web/noise-binding.mjs';
import { PAIRING_DIGEST_HEX } from '../web/fixture.mjs';
import { vector } from '../../../test-fixtures/pairing/vendored-vector.mjs';
test('spike session prologue reproduces the vendored pairing vector', () => {
  assert.equal(PAIRING_DIGEST_HEX, vector.expected.pairing_digest);
  assert.equal(toHex(sessionPrologue(fromHex(PAIRING_DIGEST_HEX))), vector.expected.session_prologue);
  assert.throws(() => sessionPrologue(new Uint8Array(31)), TypeError);
});
