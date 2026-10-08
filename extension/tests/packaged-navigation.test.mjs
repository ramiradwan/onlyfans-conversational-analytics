import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import { build } from 'esbuild';
import { openCreatorAccount } from '../ui/actions.mjs';

test('the packaged worker routes a creator click without changing settings', async () => {
  const buildSource = await readFile(new URL('../build.mjs', import.meta.url), 'utf8');
  const entry = buildSource.match(/entryPoints: \[path.join\(ROOT, '(background[^']+)'\)\]/)[1];
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../${entry}`, import.meta.url))], bundle: true, write: false, format: 'esm', platform: 'browser' });
  const callback = compiled.outputFiles[0].text.match(/openStep: ([\s\S]+?),\s*onPaired:/)[1];
  for (const existing of [true, false]) {
    const calls = [];
    const chromeApi = { tabs: {
      query: async () => existing ? [{ id: 7, windowId: 2 }] : [],
      update: async (id, options) => calls.push(['focus', id, options]),
      create: async (options) => calls.push(['create', options]),
    }, windows: { update: async (id, options) => calls.push(['window', id, options]) } };
    const open = runInNewContext(`(${callback})`, { openCreatorAccount: () => openCreatorAccount(chromeApi), openSurface: (options) => calls.push(['surface', options]) });
    assert.deepEqual(calls, []);
    await open('creator', { anchorTab: { id: 3 } });
    assert.equal(calls[0][0], existing ? 'focus' : 'create');
    assert.equal(calls.length, existing ? 2 : 1);
    calls.length = 0;
    await open('history', { anchorTab: { id: 3 } });
    assert.equal(calls[0][0], 'surface');
    assert.equal(calls[0][1].section, 'history');
  }
});
