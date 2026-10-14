import test from 'node:test';
import assert from 'node:assert/strict';
import { workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';
const journey = 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed';
test('native link carries only one canonical locator', () => {
  assert.equal(workspaceAppLink(journey), `ofca://onboarding/?journey=${journey}`);
  for (const value of [journey.toUpperCase(), `${journey}&token=x`, '', null, ['journey'], 'https://example.test']) {
    assert.throws(() => workspaceAppLink(value));
  }
});
