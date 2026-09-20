import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir, mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { chromium } from '@playwright/test';
import { SIGNER_RELEASE, signerReleaseFiles, auditInstalledSigner } from './signer-release.mjs';
import { TRAVERSAL_CHAT_IDS, TRAVERSAL_MESSAGE_IDS,
  expectedTraversalMessage } from '../tests/signer-traversal-scenario.mjs';

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = new Map(process.argv.slice(2).map((arg) => {
  const split = arg.indexOf('=');
  return split === -1 ? [arg, true] : [arg.slice(0, split), arg.slice(split + 1)];
}));
const outdir = args.has('--outdir') ? path.resolve(args.get('--outdir'))
  : await mkdtemp(path.join(tmpdir(), 'ofca-signer-worker-'));
await mkdir(outdir, { recursive: true });
const files = signerReleaseFiles(await readFile(path.join(extensionRoot, 'vendor', SIGNER_RELEASE.archive)));
const installedSigner = { files, root: path.join(extensionRoot, 'node_modules', SIGNER_RELEASE.package),
  entry: fileURLToPath(import.meta.resolve('local-authenticated-read-connector/browser-signing')) };
await auditInstalledSigner(installedSigner);
const bundlePath = path.join(outdir, 'qa-signer-worker.js');
await build({ entryPoints: [path.join(extensionRoot, 'qualification/signer-worker-fixture.mjs')],
  outfile: bundlePath, bundle: true, format: 'iife', platform: 'browser', target: 'chrome132', logLevel: 'silent' });
await auditInstalledSigner(installedSigner);
const bundle = await readFile(bundlePath);
const server = createServer((request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  if (request.url === '/worker.js') {
    response.setHeader('Content-Type', 'text/javascript'); response.end(bundle);
  } else if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><title>Synthetic signer worker qualification</title>');
  } else { response.statusCode = 404; response.end(); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
let terminatedWorkers = 0;
let reloads = 0;
const cursors = [];
const attemptedExternalUrls = [];

function durableProjection(snapshot) {
  return { identity: snapshot.identity, chats: snapshot.chats, messages: snapshot.messages,
    evidence: snapshot.evidence, outbox: snapshot.outbox,
    jobs: snapshot.jobs.map(({ lease_token, updated_at, ...job }) => job) };
}
try {
  browser = await chromium.launch({ headless: true,
    ...(args.has('--browser-executable') ? { executablePath: args.get('--browser-executable') } : {}) });
  const context = await browser.newContext();
  await context.route('**/*', async (route) => {
    if (new URL(route.request().url()).origin === origin) await route.continue();
    else { attemptedExternalUrls.push(new URL(route.request().url()).origin); await route.abort(); }
  });
  const page = await context.newPage();
  await page.goto(origin);
  await page.evaluate(() => {
    globalThis.qaNewWorker = () => { globalThis.qaWorker = new Worker('/worker.js'); };
    globalThis.qaRequest = (message) => new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('QA worker operation timed out')), 20_000);
      globalThis.qaWorker.onmessage = ({ data }) => { clearTimeout(timeout); resolve(data); };
      globalThis.qaWorker.onerror = () => { clearTimeout(timeout); reject(new Error('QA worker failed')); };
      globalThis.qaWorker.postMessage(message);
    });
  });
  const startWorker = () => page.evaluate(() => globalThis.qaNewWorker());
  const terminateWorker = async () => {
    await page.evaluate(() => { globalThis.qaWorker.terminate(); globalThis.qaWorker = null; });
    terminatedWorkers += 1;
  };
  const request = async (action, extras = {}) => {
    const result = await page.evaluate((message) => globalThis.qaRequest(message), { action, ...extras });
    assert.notEqual(result.stage, 'failed', `QA worker rejected ${action}: ${JSON.stringify(result.error)}`);
    return result;
  };
  const account = (result) => {
    const source = result.snapshot ?? result;
    reloads += source.reloads;
    cursors.push(...source.cursors);
  };

  // Commit a real page, then terminate without calling coordinator.stop or closing IDB.
  await startWorker();
  const first = await request('wake');
  assert.equal(first.stage, 'committed');
  assert.equal(first.result.status, 'progressed');
  assert.equal(first.result.pages, 1);
  assert.equal(first.snapshot.chats.length, 2);
  assert.equal(first.snapshot.jobs.find((job) => job.kind === 'inventory').cursor, '2');
  account(first);
  await terminateWorker();

  // All constructors and JS state are new; only the browser's encrypted IndexedDB survives.
  await startWorker();
  const restored = await request('snapshot');
  assert.deepEqual(durableProjection(restored.snapshot), durableProjection(first.snapshot));
  const entered = await request('wake', { pausePageCommit: true });
  assert.equal(entered.stage, 'page-transaction-entered');
  account(entered);
  await terminateWorker();

  await startWorker();
  const rolledBack = await request('snapshot');
  assert.deepEqual(durableProjection(rolledBack.snapshot), durableProjection(first.snapshot),
    'termination must roll back material, coverage, cursor, sequence and outbox atomically');
  let final;
  let completedWakes = 1;
  for (let wake = 0; wake < 10; wake += 1) {
    const resumed = await request('wake');
    assert.equal(resumed.stage, 'committed');
    assert.equal(resumed.result.status, 'progressed');
    account(resumed); completedWakes += 1; final = resumed.snapshot;
    assert.equal(final.identity.agent_stream_id, first.snapshot.identity.agent_stream_id);
    assert.equal(final.identity.account_epoch, first.snapshot.identity.account_epoch);
    assert.equal(final.jobs.find((job) => job.kind === 'inventory').generation_id,
      first.snapshot.jobs.find((job) => job.kind === 'inventory').generation_id);
    await terminateWorker();
    await startWorker();
    const persisted = await request('snapshot');
    assert.deepEqual(durableProjection(persisted.snapshot), durableProjection(final));
    if (final.jobs.find((job) => job.kind === 'inventory').phase === 'closed') break;
  }
  assert.equal(final.jobs.find((job) => job.kind === 'inventory').phase, 'closed');
  assert.ok(final.jobs.every((job) => job.cursor === null));
  const conversations = final.jobs.filter((job) => job.kind === 'conversation');
  assert.deepEqual(conversations.map((job) => job.conversation_id).sort(), TRAVERSAL_CHAT_IDS);
  assert.ok(conversations.every((job) => job.phase === 'complete'));
  assert.deepEqual(final.chats.map((chat) => chat.chat_id), TRAVERSAL_CHAT_IDS);
  assert.deepEqual(final.messages.map((message) => message.message_id), TRAVERSAL_MESSAGE_IDS);
  for (const message of final.messages) {
    const { last_source_seq, last_origin, ...material } = message;
    assert.deepEqual(material, expectedTraversalMessage(message.message_id));
  }
  assert.deepEqual(final.evidence.map((row) => row.evidence)
    .filter((row) => row.type === 'conversation.history_started')
    .map((row) => row.conversation_id).sort(), TRAVERSAL_CHAT_IDS);
  assert.equal(final.outbox.filter((row) => row.change.type === 'message.upsert').length, 5);
  assert.equal(JSON.stringify(final.outbox).includes('msg1.'), false);
  assert.deepEqual(cursors.filter((row) => row.conversationId === '101').map((row) => row.cursor),
    [null, 'msg1.101.899', 'msg1.101.898']);
  assert.equal(reloads, 1, 'persisted current signing generation avoids repeated refresh on reconstruction');
  const cancelled = await request('cancel-after-callback');
  assert.equal(cancelled.stage, 'cancel-after-callback-rolled-back');
  assert.deepEqual(attemptedExternalUrls, []);
  await terminateWorker();

  // Terminate after the encrypted refresh reservation commits but before reload.
  // Reconstructed workers must not refund it or repeat the browser side effect.
  await startWorker();
  assert.equal((await request('refresh-budget', { interruptAfterReservation: true })).stage, 'refresh-reserved');
  await terminateWorker();
  let protectedRefreshes = 0;
  for (const [advanceMs, blocked] of [[0, true], [899_999, true], [900_000, false],
    [1_800_000, false], [2_700_000, true], [3_599_999, true], [3_600_000, false]]) {
    await startWorker();
    const result = await request('refresh-budget', { advanceMs });
    assert.equal(result.blocked, blocked);
    assert.equal(result.reloads, blocked ? 0 : 1);
    protectedRefreshes += result.reloads;
    await terminateWorker();
  }
  assert.equal(protectedRefreshes, 3);
  const report = {
    scope: 'qa-source-bundle-real-indexeddb-dedicated-worker', shipping_zip_acceptance: false,
    synthetic_chrome_and_network: true, live_network: false, passed: true,
    engine: browser.version(), signer: `${SIGNER_RELEASE.package}@${SIGNER_RELEASE.version}`,
    signer_archive_sha256: SIGNER_RELEASE.sha256,
    qa_bundle_sha256: createHash('sha256').update(bundle).digest('hex'),
    completed_wakes: completedWakes, terminated_workers: terminatedWorkers,
    entered_page_transaction_rollback_verified: true, committed_page_reconstruction_verified: true,
    cancellation_after_callback_rollback_verified: true,
    refresh_budget_survives_worker_termination: true,
    refresh_budget_checks: 7,
    opaque_cursor_resume_verified: true, message_dedup_verified: true,
    chats: final.chats.length, messages: final.messages.length,
    message_outbox_transitions: final.outbox.filter((row) => row.change.type === 'message.upsert').length,
    complete_conversations: conversations.length, synthetic_bootstrap_reloads: reloads,
  };
  await writeFile(path.join(outdir, 'signer-worker-recovery-report.json'), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
