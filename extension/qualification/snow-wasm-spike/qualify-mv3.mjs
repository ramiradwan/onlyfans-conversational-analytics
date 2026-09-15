import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';
import { spawn } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createLineQueue, stopChild, within, waitForWorkerEntry } from './process-lines.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionDir = path.join(here, 'mv3-dist');
const browser = process.argv[2];
const label = process.argv[3] ?? 'current';
if (!browser) throw new Error('browser_executable_or_playwright_sentinel_required');
const binding = JSON.parse(await readFile(path.join(here, 'qualification-input.json'), 'utf8')).binding;
const remote = [];
let phase = 'setup';
const mark = (next) => { phase = next; console.log(`qualification-phase:${label}:${next}`); };

function observeRemote(context) {
  context.on('request', (request) => {
    if (/^https?:/u.test(request.url())) remote.push(request.url());
  });
}

async function launch() {
  const options = {
    args: [
      `--disable-extensions-except=${extensionDir}`,
      `--load-extension=${extensionDir}`,
      '--no-first-run',
      '--no-default-browser-check',
    ],
  };
  if (browser === 'playwright') {
    options.channel = 'chromium';
    options.headless = true;
  } else {
    options.executablePath = browser;
    options.headless = false;
  }
  mark('browser-launch');
  const context = await within('browser_launch', () => chromium.launchPersistentContext('', options), 20000);
  observeRemote(context);
  try {
    mark('service-worker-discovery');
    const worker = context.serviceWorkers()[0]
      ?? await within('service_worker_discovery', () => context.waitForEvent('serviceworker'), 15000);
    await waitForWorkerEntry(worker, '__snowSpikeRun');
    mark('service-worker-ready');
    return { context, worker };
  } catch (error) {
    await closeContext(context);
    throw error;
  }
}

async function closeContext(context) {
  if (!context) return;
  await within('browser_close', () => context.close(), 5000).catch(() => {});
}

async function runQualification() {
  mark('python-peer-start');
  const peer = spawn(
    process.env.PYTHON ?? 'python3',
    [path.join(here, 'python_peer.py'), '--binding', binding, '--websocket'],
    { stdio: ['ignore', 'pipe', 'inherit'] },
  );
  const peerLines = createLineQueue(peer, 'python_peer');
  assert.equal(await peerLines.nextLine(5000), 'ready');

  let context;
  try {
    const launched = await launch();
    context = launched.context;
    const worker = launched.worker;
    mark('fresh-state');
    const fresh = await within('fresh_state', () => worker.evaluate(() => globalThis.__snowSpikeRun({ mode: 'fresh' })), 10000);
    assert.notEqual(fresh.first, fresh.second);
    mark('python-interop');
    const interop = await within('python_interop', () => worker.evaluate(() => globalThis.__snowSpikeRun({ mode: 'interop' })), 10000);
    assert.equal(interop.result, 'passed');
    assert.equal(interop.reply, interop.expected);
  } finally {
    mark('python-peer-stop');
    await closeContext(context);
    await stopChild(peer, 'python_peer');
  }

  mark('hostile-peer-start');
  const hostile = spawn(
    process.env.PYTHON ?? 'python3',
    [path.resolve(here, '../../../tools/companion-session-spike/hostile.py')],
    { stdio: ['ignore', 'pipe', 'inherit'] },
  );
  const hostileLines = createLineQueue(hostile, 'hostile_peer');
  assert.equal(await hostileLines.nextLine(5000), 'ready');

  let hostileResult;
  let product;
  context = undefined;
  try {
    const launched = await launch();
    context = launched.context;
    const worker = launched.worker;
    mark('hostile-handshake');
    hostileResult = await within('hostile_handshake', () => worker.evaluate(() => globalThis.__snowSpikeRun({ mode: 'hostile' })), 10000);
    assert.equal(hostileResult.result, 'refused');
    mark('hostile-leak-verdict');
    const leak = await hostileLines.nextLine(5000);
    assert.equal(leak, 'application-secret-observed=false');
    mark('browser-version');
    const page = await within('browser_page', () => context.newPage(), 5000);
    const cdp = await within('browser_cdp', () => context.newCDPSession(page), 5000);
    product = (await within('browser_version', () => cdp.send('Browser.getVersion'), 5000)).product;
  } finally {
    mark('hostile-peer-stop');
    await closeContext(context);
    await stopChild(hostile, 'hostile_peer');
  }

  assert.deepEqual(remote, []);
  const report = {
    label,
    product,
    result: 'passed',
    service_worker: true,
    exact_profile: 'Noise_KK_25519_ChaChaPoly_SHA256',
    binding,
    hostile_port: hostileResult,
    remote_http_requests: remote,
  };
  await writeFile(path.join(here, `chrome-${label}.json`), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
}

try {
  await runQualification();
} catch (error) {
  const failure = { label, result: 'failed', phase, error: error instanceof Error ? error.message : String(error) };
  await writeFile(path.join(here, `chrome-${label}-failure.json`), `${JSON.stringify(failure, null, 2)}\n`).catch(() => {});
  console.error(JSON.stringify(failure));
  throw error;
}
