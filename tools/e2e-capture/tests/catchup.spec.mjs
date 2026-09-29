import { cp, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BrainProcess } from '../lib/brain.mjs';
import { establishBrowserWebAuthnSession, readBrainSummary, requestAgentPairingTicket,
  readServedRuntimeConfig } from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import { bindAgentFromBridgePage, extensionId, extensionWorker, launchExtensionBrowser,
  terminateExtensionWorker } from '../lib/extension-browser.mjs';
import { EXTENSION_DIST, assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';

async function syntheticExtension(directory) {
  await cp(EXTENSION_DIST, directory, { recursive: true });
  const original = await readFile(path.join(directory, 'background.js'), 'utf8');
  await writeFile(path.join(directory, 'production-background.mjs'), original);
  await writeFile(path.join(directory, 'background.js'), `
import { agentRuntime } from './production-background.mjs';
const initialize = agentRuntime.initialize;
agentRuntime.initialize = async (...args) => {
  const components = await initialize(...args);
  const signer = category => ({ async read(request) {
    request.signal?.throwIfAborted();
    const tabs = await chrome.tabs.query({ url: ['https://onlyfans.com/*'] });
    const tab = tabs.find(tab => tab.frozen === false && !tab.discarded);
    if (!tab) throw new Error('Synthetic tab unavailable');
    const kind = request.operation === 'identity' ? 'identity'
      : request.operation === 'conversations' ? category + '_list' : category + '_messages';
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
      func: (request, kind) => globalThis.syntheticCatchupRead(request, kind),
      args: [{ operation: request.operation, parameters: request.parameters }, kind] });
    request.signal?.throwIfAborted();
    return results[0].result;
  } });
  components.history.initial.signer = signer('history');
  components.history.catchup.signer = signer('catchup');
  return components;
};
`);
}

async function enableHistory(page) {
  return page.evaluate(async () => {
    const html = await (await fetch('/')).text();
    const document = new DOMParser().parseFromString(html, 'text/html');
    const token = document.querySelector('meta[name="csrf-token"]').content;
    const current = await fetch('/api/v1/settings/history');
    const response = await fetch('/api/v1/settings/history', { method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token, 'If-Match': current.headers.get('etag') },
      body: JSON.stringify({ desired_state: 'running', consent_policy_version: 'history-consent-v1',
        accept_consent: true, recent_window_days: 30, page_size: 100, pages_per_wake: 20,
        request_interval_ms: 500, retry_limit: 3 }) });
    return response.status;
  });
}

async function observeFreshness(page, config) {
  await page.evaluate(config => {
    globalThis.catchupStates = [];
    const socket = new WebSocket(config.FASTAPI_WS_URL);
    globalThis.catchupObserver = socket;
    socket.onopen = () => socket.send(JSON.stringify({ type: 'bridge.hello', protocol_version: '2',
      message_id: crypto.randomUUID(), payload: { auth_ticket: config.BRIDGE_AUTH_TICKET,
        requested_creator_account_id: config.CREATOR_ID, bridge_session_id: crypto.randomUUID(),
        capabilities: ['state.snapshot', 'state.delta', 'state.catchup_freshness'],
        client_version: 'catchup-e2e', last_view_revision: null } }));
    socket.onmessage = event => {
      const value = JSON.parse(event.data);
      const freshness = value.payload?.catchup_freshness
        ?? value.payload?.changes?.find(change => change.type === 'catchup_freshness.replace')?.catchup_freshness;
      if (freshness) globalThis.catchupStates.push(freshness.status);
    };
  }, config);
}

for (const enabled of [true, false]) {
  test(`unopened chats recover after downtime with catchup_enabled=${enabled}`, async () => {
    test.setTimeout(600_000);
    assertBuiltExtension(); assertBuiltSpa();
    const root = await mkdtemp(path.join(tmpdir(), 'catchup-'));
    let context, brain;
    try {
      const directory = path.join(root, 'extension');
      await syntheticExtension(directory);
      context = await launchExtensionBrowser(path.join(root, 'profile'), directory);
      const worker = await extensionWorker(context);
      const id = extensionId(worker);
      const auth = path.join(root, 'auth.sqlite3');
      brain = new BrainProcess({ authDatabasePath: auth, canonicalDatabasePath: path.join(root, 'canonical.sqlite3'),
        projectionDatabasePath: path.join(root, 'projections.sqlite3'), extensionId: id,
        environmentOverrides: { CATCHUP_ENABLED: String(enabled), CATCHUP_GRANT_MIN_INTERVAL_MINUTES: '1' } });
      await brain.start();
      const popup = await openPopup(context, id, []);
      await connectFullAnalytics(context, popup, worker);
      for (const page of context.pages()) if (page.url().startsWith('chrome-extension://')) await page.close();
      const bridge = context.pages()[0] ?? await context.newPage();
      await establishBrowserWebAuthnSession(bridge, auth);
      const pairing = await requestAgentPairingTicket(context);
      await bindAgentFromBridgePage(bridge, { extensionId: id, creatorAccountId: pairing.creatorAccountId,
        authTicket: pairing.pairingTicket, storageBootstrap: pairing.storageBootstrap });
      const platform = new SyntheticPlatform();
      const old = new Date(Date.now() - 3_600_000).toISOString();
      platform.seedCatchup('101', 'initial101', old);
      platform.seedCatchup('102', 'initial102', old);
      await platform.install(context);
      await context.exposeBinding('syntheticCatchupRead', (source, request, category) => platform.readCatchupPage(request, category));
      const reopen = async () => {
        const page = await context.newPage();
        await page.goto('https://onlyfans.com/');
        await expect.poll(() => page.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode), { timeout: 30_000 }).toBe('full');
        await page.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
        await page.evaluate(() => globalThis.fixtureOpenSocket());
        return page;
      };
      let page = await reopen();
      expect(await enableHistory(bridge)).toBe(200);
      const summary = () => readBrainSummary(context, { catchup: true });
      await expect.poll(async () => (await summary()).coverage.status, { timeout: 180_000 }).toBe('complete');
      if (enabled) await expect.poll(async () => (await summary()).catchupFreshness.status, { timeout: 180_000 }).toBe('current');
      await observeFreshness(bridge, await readServedRuntimeConfig(context));
      await expect.poll(() => bridge.evaluate(() => globalThis.catchupStates.length)).toBeGreaterThan(0);
      const before = structuredClone(platform.requestCounts);
      await page.close();
      await terminateExtensionWorker(context, worker);
      const now = new Date().toISOString();
      platform.seedCatchup('101', 'missing101', now);
      platform.seedCatchup('102', 'missing102', now);
      platform.seedCatchup('103', 'missing103', now);
      page = await reopen();
      if (enabled) {
        await expect.poll(async () => (await summary()).messageCount, { timeout: 240_000 }).toBe(5);
        await expect.poll(async () => bridge.evaluate(() => {
          const states = globalThis.catchupStates;
          const behind = states.indexOf('behind');
          const checking = states.indexOf('checking', behind + 1);
          return behind >= 0 && checking > behind && states.indexOf('current', checking + 1) > checking;
        }), { timeout: 180_000 }).toBe(true);
        expect(platform.requestCounts.catchup_list - before.catchup_list).toBe(Math.ceil(3 / 100));
        expect(platform.requestCounts.catchup_messages - before.catchup_messages).toBe(3);
        for (const chat of ['101', '102', '103']) {
          const ids = await bridge.evaluate(async chat => (await (await fetch(
            '/api/v1/conversations/' + chat + '/messages?limit=100')).json()).items.map(item => item.message_id), chat);
          expect(ids).toContain('missing' + chat);
        }
      } else {
        await expect.poll(async () => (await summary()).agentStatus, { timeout: 90_000 }).toBe('connected');
        await page.waitForTimeout(70_000);
        expect((await summary()).messageCount).toBe(2);
        expect((await summary()).catchupFreshness.status).not.toBe('current');
        expect(platform.requestCounts.catchup_list - before.catchup_list).toBe(0);
        expect(platform.requestCounts.catchup_messages - before.catchup_messages).toBe(0);
      }
      expect(page.url()).toBe('https://onlyfans.com/');
      platform.assertFailClosed();
    } finally {
      await context?.close().catch(() => {});
      await brain?.stop().catch(() => {});
      await rm(root, { recursive: true, force: true });
    }
  });
}
