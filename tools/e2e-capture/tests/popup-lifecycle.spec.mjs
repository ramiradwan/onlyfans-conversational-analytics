import { createHash, randomBytes } from 'node:crypto';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { chromium, expect, test } from '@playwright/test';

import { EXTENSION_DIST, EXTENSION_ROOT, assertBuiltExtension } from '../lib/paths.mjs';

// Drives the persistent status and setup pages against a loopback desktop
// peer. The peer moves off the production port so a running desktop app is
// never contacted.
const { build } = createRequire(path.join(EXTENSION_ROOT, 'package.json'))('esbuild');
const PRODUCTION_PORT = '17871';
const PAIRING_ID = randomBytes(32).toString('base64url');
const CHALLENGE = randomBytes(32).toString('base64url');

let temporaryRoot;
let extensionDirectory;
let server;
let desktop;

function resetDesktop(port) {
  desktop = {
    port, pairingWindowOpen: false, requests: 0, refusals: 0, results: 0, abandoned: 0, compare: null, pages: [],
    confirm() {
      const peer = desktop.compare;
      desktop.compare = null;
      desktop.results += 1;
      peer.sendText(JSON.stringify({ type: 'pair.result', pairing_id: PAIRING_ID, outcome: 'confirmed' }));
    },
  };
}

function acceptWebSocket(request, socket, handlers) {
  const accept = createHash('sha1')
    .update(`${request.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
    .digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let buffered = Buffer.alloc(0);
  let closing = false;
  const frame = (opcode, payload) => {
    const header = payload.length < 126
      ? Buffer.from([0x80 | opcode, payload.length])
      : Buffer.from([0x80 | opcode, 126, payload.length >> 8, payload.length & 0xff]);
    socket.write(Buffer.concat([header, payload]));
  };
  const peer = {
    sendText: (text) => frame(0x1, Buffer.from(text, 'utf8')),
    close(code, reason) {
      if (closing) return;
      closing = true;
      const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
      payload.writeUInt16BE(code, 0);
      payload.write(reason, 2);
      frame(0x8, payload);
      setTimeout(() => socket.end(), 1000).unref();
    },
  };
  socket.on('data', (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    while (buffered.length >= 2) {
      const opcode = buffered[0] & 0x0f;
      let length = buffered[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffered.length < 4) return;
        length = buffered.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        socket.destroy();
        return;
      }
      if (buffered.length < offset + 4 + length) return;
      const mask = buffered.subarray(offset, offset + 4);
      const payload = Buffer.alloc(length);
      for (let index = 0; index < length; index += 1) payload[index] = buffered[offset + 4 + index] ^ mask[index % 4];
      buffered = buffered.subarray(offset + 4 + length);
      if (opcode === 0x1) handlers.onText(payload.toString('utf8'), peer);
      else if (opcode === 0x8) {
        if (closing) socket.end();
        else { peer.close(1000, ''); socket.end(); }
      }
    }
  });
  socket.on('error', () => {});
  socket.on('close', () => handlers.onEnd(peer));
}

// Mirrors the desktop pairing endpoint: a request without an open desktop
// pairing window is refused before any offer.
function pairingHandlers() {
  let step = 0;
  return {
    onText(text, peer) {
      step += 1;
      if (step === 1) {
        desktop.requests += 1;
        if (JSON.parse(text).type !== 'pair.request') return peer.close(1008, 'pairing_protocol_refused');
        if (!desktop.pairingWindowOpen) {
          desktop.refusals += 1;
          return peer.close(1008, 'pairing_state_refused');
        }
        return peer.sendText(JSON.stringify({ type: 'pair.offer' }));
      }
      if (step === 2) desktop.compare = peer;
      return undefined;
    },
    onEnd(peer) {
      if (desktop.compare !== peer) return;
      desktop.compare = null;
      desktop.abandoned += 1;
    },
  };
}

function loopbackPort(port) {
  return {
    name: 'loopback-port',
    setup(context) {
      context.onLoad({ filter: /(?:local-service-endpoints|onboarding-workspace)\.mjs$/ }, async (args) => {
        const source = await readFile(args.path, 'utf8');
        const contents = source.replaceAll(`:${PRODUCTION_PORT}`, `:${port}`);
        if (contents === source) throw new Error('loopback endpoints were not rewritten');
        return { contents, loader: 'js' };
      });
    },
  };
}

const workerSource = (port) => `
  import { createCompanionClient } from './runtime/companion-client.mjs';
  import { createSurfaceOpener, registerSurfaceNavigation } from './runtime/ui-surfaces.mjs';
  import { registerOnboardingWorkspace } from './runtime/onboarding-entry.mjs';
  // This peer's consent state is already in memory; satisfy the initialization
  // boundary before the production workspace reads the current identity.
  const onboarding = registerOnboardingWorkspace({ chromeApi: chrome, consentController: { initialize: async () => {} },
    identityBridge: { currentAccountId: async () => 'synthetic-creator' } });
  const openOther = createSurfaceOpener();
  registerSurfaceNavigation(chrome, (request) => request.surface === 'setup'
    ? onboarding.open({ section: request.section }) : openOther(request));

  const PAIRING_ID = ${JSON.stringify(PAIRING_ID)};
  const CHALLENGE = ${JSON.stringify(CHALLENGE)};
  const LEGAL_ORIGIN = 'https://legal.example.test';
  const state = { mode: 'full', paired: false, cancelled: 0 };
  const identity = crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);

  function channel(accountId) {
    const value = {
      identity: { creator_account_id: accountId, pairing_id: PAIRING_ID, installation_id: 'synthetic-installation' },
      closed: false,
      close() { value.closed = true; },
      onClose() { return () => {}; },
      async rpc(method) {
        if (method === 'agent.challenge') return { challenge_id: 'synthetic-challenge', challenge: CHALLENGE,
          session_id: 'synthetic-session', expires_at: new Date(Date.now() + 60_000).toISOString() };
        if (method === 'agent.authenticate') return { creator_account_id: accountId, auth_ticket: 'synthetic-ticket',
          storage_bootstrap: 'synthetic-bootstrap' };
        if (method === 'agent.storage.unseal') return { schema: 'ofca-extension-storage-unlock/v1', creator_account_id: accountId,
          credential_kind: 'pairing', auth_ticket: 'synthetic-ticket', storage_key_base64: btoa('\\u0000'.repeat(32)) };
        if (method === 'agent.analysis.readiness') return { schema: 'ofca-analysis-readiness/v1',
          commercial_authority: 'active', analysis_admission: 'admitted' };
        throw new Error('unsupported_rpc');
      },
    };
    return value;
  }

  const client = createCompanionClient({
    allowsFull: () => state.mode === 'full',
    detectedAccountId: async () => 'synthetic-creator',
    accountDatabaseName: async () => 'unused',
    loadTrust: async () => ({}),
    loadSnow: async () => ({ generateStaticKeypair: () => new Uint8Array(64) }),
    storeFactory: async () => ({
      status: async () => ({ paired: state.paired }),
      begin: async ({ deadline }) => ({ requestId: 'synthetic-request', deadline, request: { type: 'pair.request' } }),
      acceptOffer: async () => ({ confirm: { type: 'pair.confirm', pairing_id: PAIRING_ID }, comparisonCode: '483217' }),
      cancel: async () => { state.cancelled += 1; },
      identity: async () => identity,
      forget: async () => { state.paired = false; },
      close() {},
    }),
    channelFactory: async ({ accountId }) => channel(accountId),
  });
  client.registerPopup({ onPaired: async () => {
    state.paired = true;
    client.notifySurfaces();
  } });

  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message.type === 'ofca.ui.status') reply({ ok: true, status: {
      consent: { mode: state.mode },
      phase: state.mode === 'full' ? (state.paired ? 'full' : 'identity') : state.mode,
      reload_required: false, brain_reachable: state.paired,
      preview: { message_observations: 3, chat_observations: 2, inbound_observations: 2, outbound_observations: 1 },
      delivery: { transport_state: state.paired ? 'authenticated' : 'disconnected', runtime_ready: state.paired, pending_entries: 0 },
    } });
    else if (message.type === 'ofca.legal-activation.status') reply({ ok: true, result: {
      configured: true, consent_mode: state.mode, requires_reauthorization: false,
      bindings: { public_origin: LEGAL_ORIGIN, instruments: {
        terms_of_service: { public_url: '/terms' },
        risk_disclosure: { public_url: '/risk' },
        extension_privacy_notice: { public_url: '/extension-privacy' },
      } },
      flow: { terms_event_id: 'synthetic-terms', risk_event_id: 'synthetic-risk', stage: 'complete' },
    } });
  });

  globalThis.popupLifecycle = {
    state: () => ({ ...state }),
    pairing: () => client.status(),
    set(values) { Object.assign(state, values); },

  };
`;

async function buildExtension(port) {
  extensionDirectory = path.join(temporaryRoot, 'extension');
  await cp(EXTENSION_DIST, extensionDirectory, { recursive: true });
  const plugins = [loopbackPort(port)];
  for (const name of ['popup', 'setup', 'options']) await build({
    entryPoints: [path.join(EXTENSION_ROOT, `${name}.js`)], bundle: true, format: 'iife', plugins,
    outfile: path.join(extensionDirectory, `${name}.js`), logLevel: 'silent',
  });
  await build({
    stdin: { resolveDir: EXTENSION_ROOT, contents: workerSource(port) }, bundle: true, format: 'esm', plugins,
    outfile: path.join(extensionDirectory, 'popup-lifecycle-worker.mjs'), logLevel: 'silent',
  });
  for (const bundle of ['popup.js', 'setup.js', 'options.js', 'popup-lifecycle-worker.mjs']) {
    expect(await readFile(path.join(extensionDirectory, bundle), 'utf8')).not.toContain(PRODUCTION_PORT);
  }
  const manifestPath = path.join(extensionDirectory, 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  expect(manifest.action?.default_popup).toBeUndefined();
  const policy = manifest.content_security_policy.extension_pages;
  expect(policy).toContain(`ws://127.0.0.1:${PRODUCTION_PORT}`);
  await writeFile(manifestPath, JSON.stringify({
    ...manifest,
    background: { service_worker: 'popup-lifecycle-worker.mjs', type: 'module' },
    content_security_policy: { ...manifest.content_security_policy,
      extension_pages: policy.replace(`ws://127.0.0.1:${PRODUCTION_PORT}`, `ws://127.0.0.1:${port}`) },
  }));
  const configPath = path.join(extensionDirectory, 'extension-config.json');
  const config = await readFile(configPath, 'utf8');
  expect(config).toContain(`:${PRODUCTION_PORT}/`);
  await writeFile(configPath, config.replaceAll(`:${PRODUCTION_PORT}/`, `:${port}/`));
}

async function launchBrowser() {
  const profile = await mkdtemp(path.join(temporaryRoot, 'profile-'));
  const context = await chromium.launchPersistentContext(profile, {
    channel: 'chromium',
    headless: true,
    args: [
      `--disable-extensions-except=${extensionDirectory}`,
      `--load-extension=${extensionDirectory}`,
      '--host-resolver-rules=MAP bridge.localhost 127.0.0.1',
    ],
  });
  await context.route('https://legal.example.test/**', (route) => {
    desktop.pages.push(new URL(route.request().url()).pathname);
    return route.fulfill({ contentType: 'text/html', body: '<title>Legal document fixture</title>' });
  });
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  const extensionId = new URL(worker.url()).host;
  return {
    context,
    worker,
    extensionId,
    set: (values) => worker.evaluate((next) => globalThis.popupLifecycle.set(next), values),
    state: () => worker.evaluate(() => globalThis.popupLifecycle.state()),
    pairing: () => worker.evaluate(() => globalThis.popupLifecycle.pairing()),
    // The shipping action has no dismissible popup. This secondary status page
    // remains a read-only observer while the single setup workspace owns pairing.
    async openStatusPage() {
      const page = await context.newPage();
      await page.goto(`chrome-extension://${extensionId}/popup.html`);
      await expect(page.locator('main')).toHaveAttribute('data-ready', 'true');
      return {
        text: (selector) => page.locator(selector).textContent(),
        visible: (selector) => page.locator(selector).isVisible(),
        click: (selector) => page.locator(selector).click(),
        close: () => page.close(),
      };
    },
    async close() {
      await context.close();
      await rm(profile, { recursive: true, force: true });
    },
  };
}

async function setupFrom(browser, popup) {
  const opened = browser.context.waitForEvent('page');
  await popup.click('#journey-primary');
  const page = await opened;
  await expect(page).toHaveURL(/\/setup\.html#journey=[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u);
  await expect(page.locator('main')).toHaveAttribute('data-ready', 'true');
  return page;
}

// Supplemental client lifecycle proof. Same-browser product UX now starts
// pairing automatically in App home; the real-Brain capture scenarios cover
// that route. These commands preserve the separate port ownership/cancellation
// contract without inventing an unavailable extension-side UI action.
async function sendPairingCommand(setup) {
  await setup.evaluate(() => {
    globalThis.fixturePairingPort ??= chrome.runtime.connect({ name: 'ofca.companion.pairing' });
    globalThis.fixturePairingPort.postMessage({ type: 'pair' });
  });
}

test.beforeAll(async () => {
  assertBuiltExtension();
  temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-popup-lifecycle-'));
  server = createServer((request, response) => {
    desktop.pages.push(request.url);
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>Desktop app</title>');
  });
  server.on('upgrade', (request, socket) => {
    if (request.url !== '/ws/agent/pairing') return socket.destroy();
    return acceptWebSocket(request, socket, pairingHandlers());
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  resetDesktop(server.address().port);
  await buildExtension(server.address().port);
});

test.afterAll(async () => {
  server?.closeAllConnections();
  await new Promise((resolve) => (server ? server.close(resolve) : resolve()));
  if (temporaryRoot) await rm(temporaryRoot, { recursive: true, force: true });
});

test.beforeEach(() => {
  resetDesktop(server.address().port);
});


test('connection details open in Options and repeated entry reuses that tab', async () => {
  const browser = await launchBrowser();
  try {
    await browser.set({ mode: 'full', paired: true });
    const popup = await browser.openStatusPage();
    await expect.poll(() => popup.text('#journey-title')).toBe('Ready');
    const opened = browser.context.waitForEvent('page');
    await popup.click('#open-connection');
    const options = await opened;
    await options.waitForURL(`chrome-extension://${browser.extensionId}/options.html#connection`);
    await expect(options.locator('#delivery-status')).toHaveText('Connected');
    await options.locator('#open-dashboard').click();
    await expect.poll(() => desktop.pages).toContain('/');
    expect(options.isClosed()).toBe(false);
    const reopened = await browser.openStatusPage();
    await reopened.click('#open-connection');
    await expect.poll(() => browser.context.pages().filter((page) => page.url().includes('/options.html')).length).toBe(1);
  } finally { await browser.close(); }
});

test('Full disclosure stays in the setup tab while a legal document is reviewed', async () => {
  const browser = await launchBrowser();
  try {
    await browser.set({ mode: 'preview', paired: false });
    const popup = await browser.openStatusPage();
    await expect.poll(() => popup.visible('#preview-metrics')).toBe(true);
    expect(await popup.visible('#full-disclosure')).toBe(false);
    const setup = await setupFrom(browser, popup);
    await expect(setup.locator('#full-disclosure')).toBeVisible();
    await setup.locator('#full-disclosure .extension-privacy-link').click();
    await expect.poll(() => desktop.pages).toContain('/extension-privacy');
    expect(setup.isClosed()).toBe(false);
    await setup.bringToFront();
    await expect(setup.locator('#enable-full')).toBeVisible();
    const reopened = await browser.openStatusPage();
    await reopened.click('#journey-primary');
    await expect.poll(() => browser.context.pages().filter((page) => page.url().includes('/setup.html')).length).toBe(1);
    expect((await browser.state()).mode).toBe('preview');
    expect(desktop.requests).toBe(0);
  } finally { await browser.close(); }
});

test('companion client pairs only after an explicit command and recovers from the desktop not being ready', async () => {
  const browser = await launchBrowser();
  try {
    const popup = await browser.openStatusPage();
    const setup = await setupFrom(browser, popup);
    await expect(setup.locator('#pair-companion')).toBeHidden();
    expect(desktop.requests).toBe(0);
    await sendPairingCommand(setup);
    await expect.poll(async () => (await browser.pairing()).state).toBe('desktop_not_ready');
    expect(desktop.refusals).toBe(1);
    desktop.pairingWindowOpen = true;
    await sendPairingCommand(setup);
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    expect(desktop.requests).toBe(2);
  } finally { await browser.close(); }
});

test('companion client ownership survives focus and observer closure; owner closure cancels pairing', async () => {
  const browser = await launchBrowser();
  try {
    desktop.pairingWindowOpen = true;
    const popup = await browser.openStatusPage();
    const setup = await setupFrom(browser, popup);
    await sendPairingCommand(setup);
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    const observer = await browser.openStatusPage();
    await expect.poll(() => observer.text('#journey-title')).toBe('Not connected');
    await expect.poll(() => observer.text('#journey-primary')).toBe('Continue setup');
    expect(await observer.visible('#pairing-code')).toBe(false);
    await observer.close();
    const other = await browser.context.newPage(); await other.goto('about:blank'); await other.bringToFront();
    expect((await browser.state()).cancelled).toBe(0);
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    await setup.close();
    await expect.poll(async () => (await browser.state()).cancelled).toBe(1);
    await expect.poll(() => desktop.abandoned).toBe(1);
    expect(desktop.results).toBe(0);
    const reopened = await browser.openStatusPage();
    const freshSetup = await setupFrom(browser, reopened);
    await expect(freshSetup.locator('#pairing-code')).toBeHidden();
    expect(desktop.requests).toBe(1);
  } finally { await browser.close(); }
});

test('companion client confirmation preserves setup and readiness comes from the desktop result', async () => {
  const browser = await launchBrowser();
  try {
    desktop.pairingWindowOpen = true;
    const popup = await browser.openStatusPage();
    const setup = await setupFrom(browser, popup);
    await sendPairingCommand(setup);
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    await expect(setup.locator('#journey-title')).not.toHaveText('Your analysis is ready');
    desktop.confirm();
    await expect(setup.locator('#journey-title')).toHaveText('Your analysis is ready');
    await expect(setup.locator('#pairing-code')).toBeHidden();
    await expect(setup.locator('#ready-details')).toBeVisible();
    expect(setup.isClosed()).toBe(false);
    expect(await browser.state()).toMatchObject({ paired: true, cancelled: 0 });
    const reopened = await browser.openStatusPage();
    await expect.poll(() => reopened.text('#journey-title')).toBe('Ready');
    expect(await reopened.visible('#pair-companion')).toBe(false);
  } finally { await browser.close(); }
});
