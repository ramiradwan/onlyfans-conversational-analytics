import assert from 'node:assert/strict';
import test from 'node:test';
import { parseOnboardingJson } from './json.mjs';

test('rejects duplicate keys including escaped spellings and nested objects', () => {
  for (const text of ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"x":[{"a":1,"a":2}]}']) {
    assert.throws(() => parseOnboardingJson(text));
  }
});
test('allows repeated keys in distinct objects and ignores punctuation in strings', () => {
  const value = { a: [{ x: 1 }, { x: 2 }], b: '{"x":":}\\' };
  assert.deepEqual(parseOnboardingJson(JSON.stringify(value)), value);
});
test('enforces UTF-8 bound and JSON grammar', () => {
  assert.throws(() => parseOnboardingJson('"' + 'é'.repeat(2048) + '"'));
  for (const text of ['{"a":1,}', '[1,]', 'NaN', 'undefined', '{"a" 1}', '{"x":"unterminated}']) {
    assert.throws(() => parseOnboardingJson(text));
  }
});
