import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  requiredOriginsForMode,
} from '../runtime/permission-recovery.mjs';

test('permission recovery requests only the origins required by the active mode', () => {
  assert.deepEqual(requiredOriginsForMode('preview'), ['https://onlyfans.com/*']);
  assert.deepEqual(requiredOriginsForMode('full'), [
    'https://onlyfans.com/*',
  ]);
  assert.deepEqual(requiredOriginsForMode('paused'), []);
});

test('both runtime graphs propagate the manifest version into transport construction', async () => {
  for (const file of ['../transport/agent-runtime.mjs', '../transport/read-only-agent-runtime.mjs']) {
    const source = await readFile(new URL(file, import.meta.url), 'utf8');
    assert.match(source, /getManifest\(\)\.version/);
    assert.match(source, /extensionVersion,/);
  }
});

test('popup exposes permission recovery and payload-free health fields', async () => {
  const [html, source] = await Promise.all([
    readFile(new URL('../popup.html', import.meta.url), 'utf8'),
    readFile(new URL('../popup.js', import.meta.url), 'utf8'),
  ]);
  assert.match(html, /id="restore-access"/);
  assert.match(html, /id="reload-tabs"/);
  assert.match(source, /reload_required/);
  assert.match(html, /id="capture-health"/);
  assert.match(source, /capture_drop_counts/);
  assert.match(source, /startup_error_code/);
  assert.doesNotMatch(source, /http:\/\/bridge\.localhost:17871/);
});
