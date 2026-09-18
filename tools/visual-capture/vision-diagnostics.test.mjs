import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { captureVisionDiagnostics, VISION_TYPES } from './vision-diagnostics.mjs';

function pageFixture(fail = false) {
  const types = [], screenshots = [];
  let detached = false;
  const session = {
    async send(method, value) {
      assert.equal(method, 'Emulation.setEmulatedVisionDeficiency');
      types.push(value.type);
    },
    async detach() { detached = true; },
  };
  const page = {
    context: () => ({ newCDPSession: async () => session }),
    async screenshot(options) {
      screenshots.push(options);
      if (fail) throw new Error('Capture failed');
    },
  };
  return { page, types, screenshots, detached: () => detached };
}

test('captures all four diagnostic modes and restores normal vision', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'bridge-vision-'));
  try {
    const fixture = pageFixture();
    const records = await captureVisionDiagnostics(fixture.page, directory, 'analytics-model');
    assert.deepEqual(fixture.types, [...VISION_TYPES, 'none']);
    assert.equal(records.length, 4);
    assert.ok(records.every((record) => record.diagnosticOnly && record.file.startsWith('diagnostics/vision/')));
    assert.ok(fixture.screenshots.every((options) => options.fullPage && options.animations === 'disabled'));
    assert.equal(fixture.detached(), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('restores vision and detaches even when a screenshot fails', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'bridge-vision-failure-'));
  try {
    const fixture = pageFixture(true);
    await assert.rejects(captureVisionDiagnostics(fixture.page, directory, 'failed'), /Capture failed/);
    assert.deepEqual(fixture.types, [VISION_TYPES[0], 'none']);
    assert.equal(fixture.detached(), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
