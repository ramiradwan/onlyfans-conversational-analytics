import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { retireSetupTransfer, RETIRED_SETUP_TRANSFER_ALARM, RETIRED_SETUP_TRANSFER_DATABASE } from '../runtime/setup-transfer-retirement.mjs';

const run = promisify(execFile);
const EXTENSION_ROOT = fileURLToPath(new URL('..', import.meta.url));
const RELEASE = fileURLToPath(new URL('./fixtures/customer-release.synthetic.json', import.meta.url));
const CSP = "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871; frame-ancestors 'none';";

test('a configured build names the hosted origin in no manifest value', async () => {
  const release = JSON.parse(await readFile(RELEASE, 'utf8'));
  const hosted = release.hosted_api_origin;
  const outdir = await mkdtemp(path.join(tmpdir(), 'ofca-workspace-hosts-'));
  try {
    await run(process.execPath, [path.join(EXTENSION_ROOT, 'build.mjs'), `--customer-release-config=${RELEASE}`,
      `--outdir=${outdir}`], { cwd: EXTENSION_ROOT });
    const raw = await readFile(path.join(outdir, 'manifest.json'), 'utf8');
    const manifest = JSON.parse(raw);
    assert.equal(manifest.content_security_policy.extension_pages, CSP);
    assert.deepEqual(manifest.externally_connectable, { matches: ['http://bridge.localhost:17871/*'] });
    assert.deepEqual(manifest.host_permissions, ['http://bridge.localhost/*']);
    assert.deepEqual(manifest.optional_host_permissions, ['https://onlyfans.com/*']);
    assert.deepEqual(manifest.permissions, ['alarms', 'identity', 'scripting', 'storage', 'unlimitedStorage']);
    assert.deepEqual(manifest.optional_permissions, ['webRequest']);
    assert.equal(raw.includes(new URL(hosted).host), false, 'no manifest value names the hosted origin');
    // The sign-in popup still validates its Brain-supplied URL against this origin.
    const meta = JSON.parse(await readFile(path.join(outdir, 'build-meta.json'), 'utf8'));
    assert.equal(meta.workspace_hosted_origin, hosted);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
});

test('an update removes the retired setup-code receive state', async () => {
  const listeners = [], cleared = [], removed = [], deleted = [];
  const chromeApi = { runtime: { onInstalled: { addListener: fn => listeners.push(fn) } },
    alarms: { async clear(name) { cleared.push(name); return true; } },
    storage: { session: { async remove(key) { removed.push(key); } } } };
  retireSetupTransfer({ chromeApi, indexedDb: { deleteDatabase: name => deleted.push(name) } });
  listeners[0]({ reason: 'install' });
  listeners[0]({ reason: 'chrome_update' });
  assert.deepEqual([cleared, removed, deleted], [[], [], []]);
  listeners[0]({ reason: 'update' });
  await Promise.resolve();
  assert.deepEqual(cleared, [RETIRED_SETUP_TRANSFER_ALARM]);
  assert.deepEqual(removed, ['onboarding_code_entry_v1']);
  assert.deepEqual(deleted, [RETIRED_SETUP_TRANSFER_DATABASE]);
});
