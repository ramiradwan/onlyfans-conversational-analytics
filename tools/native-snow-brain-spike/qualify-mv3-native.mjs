import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { chromium } = require(path.join(here, '../../extension/node_modules/@playwright/test'));
const extensionDir = path.join(here, 'mv3-dist');
const browser = process.argv[2] ?? 'playwright';
const serverExecutable = process.argv[3];
if (!serverExecutable) throw new Error('frozen_server_executable_required');
const serverReport = path.join(here, 'native-server-results.json');
const browserReport = path.join(here, 'mv3-browser-results.json');
const finalReport = path.join(here, 'mv3-native-results.json');

function lineQueue(child) {
  let buffer = '', waiters = [];
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    buffer += chunk;
    while (buffer.includes('\n')) {
      const i = buffer.indexOf('\n');
      const line = buffer.slice(0, i).trim();
      buffer = buffer.slice(i + 1);
      const waiter = waiters.shift();
      if (waiter) waiter.resolve(line);
    }
  });
  return { next(timeout = 10000) { return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server_line_timeout')), timeout);
    waiters.push({resolve: value => { clearTimeout(timer); resolve(value); }, reject});
  }); }};
}
function exited(child, timeout = 30000) {
  if (child.exitCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server_exit_timeout')), timeout);
    child.once('exit', code => { clearTimeout(timer); resolve(code); });
  });
}
async function waitWorker(context) {
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker', {timeout: 15000});
  const start = Date.now();
  while (Date.now() - start < 10000) {
    if (await worker.evaluate(() => typeof globalThis.__nativeSnowRun === 'function')) return worker;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('service_worker_entry_timeout');
}

const server = spawn(serverExecutable, ['--report', serverReport], {stdio: ['ignore', 'pipe', 'inherit']});
const lines = lineQueue(server);
assert.equal(await lines.next(), 'ready');
let context;
try {
  const options = {
    args: [`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`, '--no-first-run', '--no-default-browser-check'],
    headless: true,
  };
  if (browser === 'playwright') options.channel = 'chromium'; else options.executablePath = browser;
  context = await chromium.launchPersistentContext('', options);
  const worker = await waitWorker(context);
  const browserResults = await worker.evaluate(() => globalThis.__nativeSnowRun());
  await writeFile(browserReport, JSON.stringify(browserResults, null, 2) + '\n');
  await context.close(); context = undefined;
  const code = await exited(server);
  const nativeResults = JSON.parse(await readFile(serverReport, 'utf8'));
  assert.equal(code, 0);

  assert.equal(browserResults.positive.result, 'passed');
  assert.equal(nativeResults.cases.positive.result, 'passed');
  assert.equal(browserResults.positive.handshakeHash, nativeResults.cases.positive.handshake_hash);
  assert.notEqual(browserResults['fresh-1'].first, browserResults['fresh-2'].first);
  assert.notEqual(nativeResults.cases['fresh-1'].responder_handshake_sha256, nativeResults.cases['fresh-2'].responder_handshake_sha256);
  for (const [name, result] of Object.entries(browserResults)) assert.equal(result.result, 'passed', `browser:${name}`);
  for (const [name, result] of Object.entries(nativeResults.cases)) assert.equal(result.result, 'passed', `native:${name}`);
  assert.deepEqual(nativeResults.missing, []);

  const report = {
    result: 'passed',
    exact_profile: nativeResults.suite,
    browser_product: await (async () => {
      const probe = await chromium.launch({headless:true});
      try { return probe.version(); } finally { await probe.close(); }
    })(),
    handshake_hash: browserResults.positive.handshakeHash,
    fresh_agent_first_messages_differ: browserResults['fresh-1'].first !== browserResults['fresh-2'].first,
    fresh_brain_responses_differ: nativeResults.cases['fresh-1'].responder_handshake_sha256 !== nativeResults.cases['fresh-2'].responder_handshake_sha256,
    authorization_plaintext_limit: nativeResults.authorization_plaintext_limit,
    ordinary_plaintext_limit: nativeResults.ordinary_plaintext_limit,
    browser_cases: browserResults,
    native_cases: nativeResults.cases,
    frozen_server_sha256: createHash('sha256').update(await readFile(serverExecutable)).digest('hex'),
  };
  await writeFile(finalReport, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
} finally {
  if (context) await context.close().catch(() => {});
  if (server.exitCode === null) server.kill();
}