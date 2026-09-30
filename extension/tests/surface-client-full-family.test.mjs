import test from 'node:test';
import assert from 'node:assert/strict';

import * as surfaceClient from '../ui/surface-client.mjs';

test('paused Full remains in the desktop-control family after worker replacement', () => {
  const { isFullFamilyStatus } = surfaceClient;
  assert.equal(isFullFamilyStatus({ consent: { mode: 'full', resume_mode: null } }), true);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'paused', resume_mode: 'full' } }), true);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'paused', resume_mode: 'preview' } }), false);
  assert.equal(isFullFamilyStatus({ consent: { mode: 'preview', resume_mode: null } }), false);
  assert.equal(isFullFamilyStatus(null), false);
});

test('paused Full refresh retains desktop reachability', async () => {
  const { createSurfaceClient } = await import('../ui/surface-client.mjs');
  const { DESKTOP_LINK_STORAGE_KEY } = await import('../runtime/desktop-port.mjs');
  const previous = globalThis.chrome;
  globalThis.chrome = {
    runtime: { async sendMessage() { return { ok: true, status: { consent: { mode: 'paused', resume_mode: 'full' } } }; } },
    storage: { session: { async get() { return { [DESKTOP_LINK_STORAGE_KEY]: 1 }; } } },
  };
  try {
    const client = createSurfaceClient(() => {}, error => { throw error; });
    await client.refresh();
    assert.equal(client.model.desktopRuntimeReachable, true);
  } finally { globalThis.chrome = previous; }
});
