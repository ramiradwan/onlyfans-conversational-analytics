import test from 'node:test';
import assert from 'node:assert/strict';

import { isFullFamilyStatus } from '../ui/surface-client.mjs';

test('paused Full remains in the desktop-control family after worker replacement', () => {
  assert.equal(isFullFamilyStatus({ consent: { mode: 'full', resume_mode: null } }), true);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'paused', resume_mode: 'full' } }), true);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'paused', resume_mode: 'preview' } }), false);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'preview', resume_mode: null } }), false);
  assert.equal(isFullFamilyStatus(null), false);
});
