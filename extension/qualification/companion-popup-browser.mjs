import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect } from '@playwright/test';
import { build } from 'esbuild';

// UI composition qualification with a synthetic comparison peer. The native
// production transport gate separately exercises cryptographic admission.
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const temporary = await mkdtemp(path.join(tmpdir(), 'ofca-companion-popup-'));
const directory = path.join(temporary, 'extension');
let context;
try {
  await cp(path.join(root, 'dist'), directory, { recursive: true });
  const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
  await build({ entryPoints: [path.join(root, 'popup.js')], bundle: true, format: 'iife', outfile: path.join(directory, 'popup.js') });
  await build({ stdin: { resolveDir: root, contents: `
    import { createCompanionClient } from './runtime/companion-client.mjs';
    const state = { cancelled: 0, receivedPair: 0, senders: [] };
    globalThis.popupQualificationState = () => structuredClone(state);
    chrome.runtime.onConnect.addListener((port) => state.senders.push(port.sender.url));
    const client = createCompanionClient({ allowsFull: () => true, detectedAccountId: async () => 'synthetic-creator',
      accountDatabaseName: async () => 'unused', loadTrust: async () => ({}),
      loadSnow: async () => ({ generateStaticKeypair: () => new Uint8Array(64) }),
      storeFactory: async () => ({ status: async () => ({ paired: false }),
        begin: async () => ({ requestId: 'synthetic', deadline: Math.floor(Date.now() / 1000) + 300, request: { type: 'pair.request' } }),
        acceptOffer: async () => ({ confirm: { type: 'pair.confirm' }, comparisonCode: '123456' }),
        cancel: async () => { state.cancelled++; },
      }),
      wireFactory: async (_url, { signal }) => {
        state.receivedPair++; let phase = 0;
        return { close() {}, send: async () => {}, receive: async () => {
          if (++phase === 1) return '{}';
          return new Promise((resolve) => signal.addEventListener('abort', () => resolve('cancelled'), { once: true }));
        } };
      },
    });
    client.registerPopup();
    chrome.runtime.onMessage.addListener((message, _sender, reply) => {
      if (message.type === 'ofca.ui.status') reply({ ok: true, status: {
        consent: { mode: 'full' }, phase: 'identity', preview: { message_observations: 0, chat_observations: 0, inbound_observations: 0, outbound_observations: 0 },
        brain_reachable: false, delivery: { transport_state: 'disconnected', runtime_ready: false, pending_entries: 0 },
      } });
      else if (message.type === 'ofca.legal-activation.status') reply({ ok: true, result: {
        configured: true, consent_mode: 'full', flow: { terms_event_id: 'synthetic', risk_event_id: 'synthetic', stage: 'complete' },
      } });
    });
  ` }, bundle: true, format: 'esm', outfile: path.join(directory, 'popup-qualification.mjs') });
  await writeFile(path.join(directory, 'manifest.json'), JSON.stringify({ ...manifest,
    background: { service_worker: 'popup-qualification.mjs', type: 'module' } }));
  const browser = process.argv[2];
  context = await chromium.launchPersistentContext(path.join(temporary, 'profile'), {
    ...(browser && browser !== 'playwright' ? { executablePath: browser } : { channel: 'chromium' }),
    headless: true, args: [`--disable-extensions-except=${directory}`, `--load-extension=${directory}`],
  });
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  const id = new URL(worker.url()).host;
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  await expect(popup.locator('#pair-companion')).toBeVisible();
  const opened = context.waitForEvent('page');
  await popup.locator('#pair-companion').click();
  const comparison = await opened;
  await comparison.waitForURL(`chrome-extension://${id}/popup.html#pairing`);
  await expect(comparison.locator('#pairing-code')).toHaveText('123 456');
  await popup.bringToFront();
  await expect(comparison.locator('#pairing-code')).toHaveText('123 456');
  const state = () => worker.evaluate(() => globalThis.popupQualificationState());
  assert.equal((await state()).cancelled, 0);
  assert.equal((await state()).receivedPair, 1);
  assert.ok((await state()).senders.includes(`chrome-extension://${id}/popup.html#pairing`));
  await comparison.close();
  await expect.poll(async () => (await state()).cancelled).toBe(1);
  const cdp = await context.newCDPSession(popup);
  const version = await cdp.send('Browser.getVersion');
  const report = { result: 'passed', browser: version.product, scope: 'synthetic_peer_popup_composition',
    scenarios: ['persistent_comparison_window', 'focus_change_preserves_comparison', 'exact_packaged_sender_admission', 'window_close_cancels_pairing'] };
  if (process.argv[3]) await writeFile(process.argv[3], JSON.stringify(report, null, 2) + '\n');
  process.stdout.write(JSON.stringify(report) + '\n');
} finally {
  await context?.close();
  if (!path.resolve(temporary).startsWith(path.resolve(tmpdir()) + path.sep + 'ofca-companion-popup-')) throw new Error('qualification_cleanup_path_refused');
  await rm(temporary, { recursive: true, force: true });
}
