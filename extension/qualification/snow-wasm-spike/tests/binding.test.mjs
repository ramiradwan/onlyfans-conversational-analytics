import test from 'node:test'; import assert from 'node:assert/strict';
import { receiptBinding, toHex } from '../web/noise-binding.mjs';
import { FIXED_CLAIMS, EXPECTED_BINDING_HEX } from '../web/fixture.mjs';
test('browser canonical binding independently reproduces Python vector', async () => {
  assert.equal(toHex(await receiptBinding(FIXED_CLAIMS)), EXPECTED_BINDING_HEX);
});
