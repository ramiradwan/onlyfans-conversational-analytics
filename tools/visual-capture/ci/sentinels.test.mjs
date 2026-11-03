import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const root = new URL('../', import.meta.url);

test('the synthetic probe admits exactly the approved locked Playwright', async () => {
  const manifest = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
  const lock = JSON.parse(await readFile(new URL('package-lock.json', root), 'utf8'));
  const installed = JSON.parse(await readFile(new URL('node_modules/playwright/package.json', root), 'utf8'));
  const probe = await readFile(new URL('ci/sentinels.mjs', root), 'utf8');

  assert.equal(manifest.devDependencies.playwright, '1.64.1');
  assert.equal(lock.packages['node_modules/playwright'].version, '1.64.1');
  assert.equal(installed.version, '1.64.1');
  assert.ok(probe.includes("assert.equal(packageInfo.version, '1.64.1')"), 'probe keeps the approved version admission');
});
