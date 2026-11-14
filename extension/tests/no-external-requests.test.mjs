import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

// The extension's only network peers are Brain on this computer and the
// OnlyFans pages it observes. It never contacts the hosted service.
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const SKIPPED = new Set(['node_modules', 'dist', 'tests', 'qualification', 'test-fixtures', 'vendor', 'qa']);
const BUILD_ONLY = new Set(['build.mjs']);
const ORIGINS = new Set(['https://onlyfans.com', 'http://bridge.localhost', 'http://bridge.localhost:17871', 'ws://127.0.0.1:17871']);
// Each network primitive is allowed only where it reaches the extension's own
// package or Brain's loopback socket. The page hook wraps OnlyFans' own calls.
const PRIMITIVES = [
  ['fetch(', ['runtime/companion-client.mjs', 'ui/surface-client.mjs', 'page-hook.js']],
  ['XMLHttpRequest', ['page-hook.js']],
  ['new WebSocket(', ['runtime/customer-journey.mjs', 'transport/companion-channel.mjs', 'runtime/sign-in-timing.mjs']],
  ['EventSource(', []],
  ['sendBeacon', []],
  ['importScripts(', []],
  ['onMessageExternal', []],
  ['onConnectExternal', ['runtime/desktop-port.mjs']],
];

async function sources(directory = ROOT, prefix = '') {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      if (!SKIPPED.has(entry.name)) found.push(...await sources(path.join(directory, entry.name), relative));
    } else if (/\.(m?js|html)$/u.test(entry.name) && !BUILD_ONLY.has(relative)) {
      found.push([relative, await readFile(path.join(directory, entry.name), 'utf8')]);
    }
  }
  return found;
}

test('extension sources name no network peer other than Brain and OnlyFans', async () => {
  const files = await sources();
  assert.ok(files.length > 40, 'the scan reads the shipped sources');
  for (const [file, text] of files) {
    for (const match of text.matchAll(/\b(?:https?|wss?):\/\/[^\s'"`)<>/;]*/gu)) {
      const origin = match[0];
      // The identity return URI is derived from the extension's own id.
      if (origin === 'https://${chromeApi.runtime.id}.chromiumapp.org') continue;
      assert.ok(ORIGINS.has(origin), `${file} names ${origin}`);
    }
  }
});

test('network primitives appear only at their reviewed call sites', async () => {
  for (const [file, text] of await sources()) {
    for (const [primitive, allowed] of PRIMITIVES) {
      if (text.includes(primitive)) assert.ok(allowed.includes(file), `${file} uses ${primitive}`);
    }
    assert.equal(/<form\b(?![^>]*method="dialog")[^>]*>/u.test(text), false, `${file} has a submitting form`);
  }
});

test('the hosted origin reaches only the sign-in popup owner', async () => {
  const users = (await sources()).filter(([, text]) => text.includes('onboardingHostedOrigin')).map(([file]) => file).sort();
  assert.deepEqual(users, ['background-read-only.js', 'background.js', 'runtime/onboarding-release-config.mjs']);
  for (const file of ['background.js', 'background-read-only.js']) {
    const text = (await readFile(path.join(ROOT, file), 'utf8'));
    const uses = [...text.matchAll(/onboardingHostedOrigin/gu)].length;
    assert.equal(uses, 2, `${file} imports it once and passes it to browser auth once`);
    assert.match(text, /createBrowserAuth\(\{ chromeApi: chrome, hostedOrigin: onboardingHostedOrigin, onRemoved: observePopupRemoval \}\)/u);
  }
});

test('no setup-code receive handler or code entry remains', async () => {
  for (const [file, text] of await sources()) {
    assert.equal(text.includes('ofca.setup-transfer.v1'), false, file);
    assert.equal(text.includes('setup-transfer-form'), false, file);
    assert.equal(text.includes('registerSetupTransfer'), false, file);
  }
});
