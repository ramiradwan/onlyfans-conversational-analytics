import { createHash, randomBytes } from 'node:crypto';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { chromium, expect, test } from '@playwright/test';

import { EXTENSION_DIST, EXTENSION_ROOT, assertBuiltExtension } from '../lib/paths.mjs';

// Drives the real toolbar popup and persistent setup tab against a loopback desktop
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
      context.onLoad({ filter: /local-service-endpoints\.mjs$/ }, async (args) => {
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
  import { registerSurfaceNavigation } from './runtime/ui-surfaces.mjs';
  registerSurfaceNavigation();

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
  client.registerPopup({ onPaired: async () => { state.paired = true; } });

  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message.type === 'ofca.ui.status') reply({ ok: true, status: {
      consent: { mode: state.mode },
      phase: state.mode === 'full' ? (state.paired ? 'full' : 'identity') : state.mode,
      reload_required: false,
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
    set(values) { Object.assign(state, values); },
    async openToolbarPopup() {
      const [normal] = await chrome.windows.getAll({ windowTypes: ['normal'] });
      await chrome.action.openPopup({ windowId: normal.id });
    },
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

async function connectTarget(webSocketDebuggerUrl) {
  const socket = new WebSocket(webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  const waiting = new Map();
  let next = 0;
  let isClosed = false;
  let markClosed;
  const closed = new Promise((resolve) => { markClosed = resolve; });
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    const waiter = waiting.get(message.id);
    if (!waiter) return;
    waiting.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error.message));
    else waiter.resolve(message.result);
  };
  socket.onclose = () => {
    isClosed = true;
    for (const waiter of waiting.values()) waiter.reject(new Error('popup_closed'));
    waiting.clear();
    markClosed();
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    if (isClosed) return reject(new Error('popup_closed'));
    next += 1;
    waiting.set(next, { resolve, reject });
    socket.send(JSON.stringify({ id: next, method, params }));
    return undefined;
  });
  const evaluate = async (expression) => {
    const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (exceptionDetails) throw new Error(exceptionDetails.text);
    return result.value;
  };
  const query = (selector) => `document.querySelector(${JSON.stringify(selector)})`;
  return {
    closed,
    isClosed: () => isClosed,
    evaluate,
    text: (selector) => evaluate(`${query(selector)}?.textContent.trim() ?? null`),
    visible: (selector) => evaluate(`(() => { const element = ${query(selector)}; return Boolean(element) && element.getClientRects().length > 0; })()`),
    view: () => evaluate(`document.querySelector('main').dataset.view`),
    async click(selector) {
      await expect.poll(() => evaluate(
        `(() => { const element = ${query(selector)}; return Boolean(element) && !element.disabled && element.getClientRects().length > 0; })()`,
      )).toBe(true);
      const { x, y } = await evaluate(
        `(() => { const element = ${query(selector)}; element.scrollIntoView({ block: 'center' }); const box = element.getClientRects()[0]; return { x: box.left + box.width / 2, y: box.top + box.height / 2 }; })()`,
      );
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 }).catch(() => {});
    },
    async close() {
      await evaluate('window.close()').catch(() => {});
      await closed;
    },
  };
}

async function launchBrowser() {
  const profile = await mkdtemp(path.join(temporaryRoot, 'profile-'));
  const context = await chromium.launchPersistentContext(profile, {
    channel: 'chromium',
    headless: true,
    args: [
      `--disable-extensions-except=${extensionDirectory}`,
      `--load-extension=${extensionDirectory}`,
      '--remote-debugging-port=0',
      '--host-resolver-rules=MAP bridge.localhost 127.0.0.1',
    ],
  });
  await context.route('https://legal.example.test/**', (route) => {
    desktop.pages.push(new URL(route.request().url()).pathname);
    return route.fulfill({ contentType: 'text/html', body: '<title>Legal document fixture</title>' });
  });
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  const extensionId = new URL(worker.url()).host;
  let debugPort = null;
  await expect.poll(async () => {
    debugPort = await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')
      .then((value) => value.split('\n')[0].trim(), () => null);
    return debugPort;
  }).toMatch(/^\d+$/);
  const seenPopups = new Set();
  return {
    context,
    worker,
    extensionId,
    set: (values) => worker.evaluate((next) => globalThis.popupLifecycle.set(next), values),
    state: () => worker.evaluate(() => globalThis.popupLifecycle.state()),
    // The toolbar popup has no Playwright page, so it is driven over its own DevTools target.
    async openToolbarPopup() {
      await worker.evaluate(() => globalThis.popupLifecycle.openToolbarPopup());
      const url = `chrome-extension://${extensionId}/popup.html`;
      let target = null;
      await expect.poll(async () => {
        const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
        target = targets.find((entry) => entry.url === url && !seenPopups.has(entry.id)) ?? null;
        return target !== null;
      }).toBe(true);
      seenPopups.add(target.id);
      return connectTarget(target.webSocketDebuggerUrl);
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
  await page.waitForURL(new RegExp(`/setup\\.html(?:#full)?$`));
  await expect(page.locator('main')).toHaveAttribute('data-ready', 'true');
  return page;
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
    const popup = await browser.openToolbarPopup();
    await expect.poll(() => popup.text('#journey-title')).toBe('Your analysis is ready');
    const opened = browser.context.waitForEvent('page');
    await popup.click('#open-connection');
    const options = await opened;
    await options.waitForURL(`chrome-extension://${browser.extensionId}/options.html#connection`);
    await expect(options.locator('#delivery-status')).toHaveText('Connected');
    await options.locator('#open-dashboard').click();
    await expect.poll(() => desktop.pages).toContain('/');
    expect(options.isClosed()).toBe(false);
    const reopened = await browser.openToolbarPopup();
    await reopened.click('#open-connection');
    await expect.poll(() => browser.context.pages().filter((page) => page.url().includes('/options.html')).length).toBe(1);
  } finally { await browser.close(); }
});

test('Full disclosure stays in the setup tab while a legal document is reviewed', async () => {
  const browser = await launchBrowser();
  try {
    await browser.set({ mode: 'preview', paired: false });
    const popup = await browser.openToolbarPopup();
    await expect.poll(() => popup.visible('#preview-metrics')).toBe(true);
    expect(await popup.visible('#full-disclosure')).toBe(false);
    const setup = await setupFrom(browser, popup);
    await expect(setup.locator('#full-disclosure')).toBeVisible();
    await setup.locator('#full-disclosure .extension-privacy-link').click();
    await expect.poll(() => desktop.pages).toContain('/extension-privacy');
    expect(setup.isClosed()).toBe(false);
    await setup.bringToFront();
    await expect(setup.locator('#enable-full')).toBeVisible();
    const reopened = await browser.openToolbarPopup();
    await reopened.click('#journey-primary');
    await expect.poll(() => browser.context.pages().filter((page) => page.url().includes('/setup.html')).length).toBe(1);
    expect((await browser.state()).mode).toBe('preview');
    expect(desktop.requests).toBe(0);
  } finally { await browser.close(); }
});

test('pairing starts only on explicit setup action and recovers from the desktop not being ready', async () => {
  const browser = await launchBrowser();
  try {
    const popup = await browser.openToolbarPopup();
    const setup = await setupFrom(browser, popup);
    await expect(setup.locator('#pair-companion')).toBeVisible();
    expect(desktop.requests).toBe(0);
    await setup.locator('#pair-companion').click();
    await expect(setup.locator('#journey-title')).toHaveText('Continue in the desktop app');
    await expect(setup.locator('#journey-primary')).toHaveText('Open desktop app settings');
    await expect(setup.locator('#pair-companion')).toHaveText('Pair device');
    expect(desktop.refusals).toBe(1);
    await setup.locator('#journey-primary').click();
    await expect.poll(() => desktop.pages).toContain('/settings');
    desktop.pairingWindowOpen = true;
    await setup.locator('#pair-companion').click();
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    expect(desktop.requests).toBe(2);
  } finally { await browser.close(); }
});

test('focus changes and closing an observing popup preserve pairing; closing setup cancels it', async () => {
  const browser = await launchBrowser();
  try {
    desktop.pairingWindowOpen = true;
    const popup = await browser.openToolbarPopup();
    const setup = await setupFrom(browser, popup);
    await setup.locator('#pair-companion').click();
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    const observer = await browser.openToolbarPopup();
    await expect.poll(() => observer.text('#journey-title')).toBe('Connection in progress');
    expect(await observer.visible('#pairing-code')).toBe(false);
    await observer.close();
    const other = await browser.context.newPage(); await other.goto('about:blank'); await other.bringToFront();
    expect((await browser.state()).cancelled).toBe(0);
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    await setup.close();
    await expect.poll(async () => (await browser.state()).cancelled).toBe(1);
    await expect.poll(() => desktop.abandoned).toBe(1);
    expect(desktop.results).toBe(0);
    const reopened = await browser.openToolbarPopup();
    const freshSetup = await setupFrom(browser, reopened);
    await expect(freshSetup.locator('#pairing-code')).toBeHidden();
    expect(desktop.requests).toBe(1);
  } finally { await browser.close(); }
});

test('a confirmed connection stays in setup and readiness comes from the desktop result', async () => {
  const browser = await launchBrowser();
  try {
    desktop.pairingWindowOpen = true;
    const popup = await browser.openToolbarPopup();
    const setup = await setupFrom(browser, popup);
    await setup.locator('#pair-companion').click();
    await expect(setup.locator('#pairing-code')).toHaveText('483 217');
    await expect(setup.locator('#journey-title')).not.toHaveText('Your analysis is ready');
    desktop.confirm();
    await expect(setup.locator('#journey-title')).toHaveText('Your analysis is ready');
    await expect(setup.locator('#pairing-code')).toBeHidden();
    await expect(setup.locator('#ready-details')).toBeVisible();
    expect(setup.isClosed()).toBe(false);
    expect(await browser.state()).toMatchObject({ paired: true, cancelled: 0 });
    const reopened = await browser.openToolbarPopup();
    await expect.poll(() => reopened.text('#journey-title')).toBe('Your analysis is ready');
    expect(await reopened.visible('#pair-companion')).toBe(false);
  } finally { await browser.close(); }
});
