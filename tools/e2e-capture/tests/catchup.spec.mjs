import { cp, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BrainProcess } from '../lib/brain.mjs';
import { establishBrowserWebAuthnSession, readBrainSummary, requestAgentPairingTicket,
  readServedRuntimeConfig } from '../lib/brain-probe.mjs';
import {
  ONLYFANS_ORIGIN_PATTERN,
  acceptNativeHostPermissionPrompt,
  connectFullAnalytics,
  openManageExtension,
  openPopup,
} from '../lib/consent-ui.mjs';
import { bindAgentFromBridgePage, extensionId, extensionWorker, launchExtensionBrowser,
  terminateExtensionWorker } from '../lib/extension-browser.mjs';
import { EXTENSION_DIST, assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';
import { installCatchupShim, withCatchupDiagnostics } from '../lib/catchup-diagnostics.mjs';

async function syntheticExtension(directory) {
  await cp(EXTENSION_DIST, directory, { recursive: true });
  const trust = JSON.parse(await readFile(
    new URL('../../../contracts/companion-pairing-v1/trust-set.json', import.meta.url),
    'utf8',
  ));
  if (trust.production_usable !== false || !Array.isArray(trust.keys)
    || trust.keys.length === 0 || !trust.keys.every(entry => entry?.fixture_only === true)) {
    throw new Error('The pinned E2E pairing trust fixture is not explicitly test-only.');
  }
  await writeFile(path.join(directory, 'companion-grant-trust.json'),
    `${JSON.stringify({ ...trust, production_usable: true }, null, 2)}\n`);
  const original = await readFile(path.join(directory, 'background.js'), 'utf8');
  await writeFile(path.join(directory, 'production-background.mjs'), original);
  await writeFile(path.join(directory, 'background.js'), `
import { agentRuntime, consentController } from './production-background.mjs';
(${installCatchupShim.toString()})(agentRuntime, chrome, globalThis, () => consentController.status());
`);
}

async function grantHistoryPermission(context, popup, worker) {
  const granted = await worker.evaluate(async (origin) => chrome.permissions.contains({
    permissions: ['webRequest'],
    origins: [origin],
  }), ONLYFANS_ORIGIN_PATTERN);
  if (granted) return;

  const options = await openManageExtension(popup);
  await expect(options.getByRole('button', { name: 'Allow message history' })).toBeVisible();
  const settingsPage = context.waitForEvent('page');
  await options.getByRole('button', { name: 'Allow message history' }).click();
  const hasPermission = () => worker.evaluate(async (origin) => chrome.permissions.contains({
    permissions: ['webRequest'],
    origins: [origin],
  }), ONLYFANS_ORIGIN_PATTERN);
  // Chromium may grant webRequest without a native confirmation once the
  // OnlyFans host permission is already present. Automate the native prompt
  // only when the permission is still pending after the click.
  let grantedWithoutPrompt = false;
  try {
    await expect.poll(hasPermission, { timeout: 1_500 }).toBe(true);
    grantedWithoutPrompt = true;
  } catch {}
  if (!grantedWithoutPrompt) await acceptNativeHostPermissionPrompt(context);
  await expect.poll(hasPermission, {
    timeout: 12_000,
    message: 'History permission was not granted through the extension UI.',
  }).toBe(true);
  const opened = await settingsPage;
  if (opened !== popup && opened !== options) await opened.close().catch(() => undefined);
  await options.close().catch(() => undefined);
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

test.describe.configure({ retries: 0 });

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
      const historyPopup = await openPopup(context, id, []);
      await grantHistoryPermission(context, historyPopup, worker);
      await historyPopup.close();
      const platform = new SyntheticPlatform();
      const old = new Date(Date.now() - 3_600_000).toISOString();
      platform.seedCatchup('101', 'initial101', old);
      platform.seedCatchup('102', 'initial102', old);
      await platform.install(context);
      await context.exposeBinding('syntheticCatchupRead', (source, request, category) => platform.readCatchupPage(request, category));
      let lastSummary = null;
      const summary = async () => (lastSummary = await readBrainSummary(context, { catchup: true }));
      const poll = (read, expected, timeout = 180_000) => withCatchupDiagnostics(
        () => expect.poll(read, { timeout }).toBe(expected),
        { summary: () => lastSummary ?? summary(), platform, context },
      );
      const reopen = async () => {
        const page = await context.newPage();
        await page.goto('https://onlyfans.com/');
        await poll(() => page.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode), 'full', 30_000);
        await page.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
        await page.evaluate(() => globalThis.fixtureOpenSocket());
        return page;
      };
      let page = await reopen();
      expect(await enableHistory(bridge)).toBe(200);
      await poll(async () => (await summary()).coverage.status, 'complete', 60_000);
      if (enabled) await poll(async () => (await summary()).catchupFreshness?.status, 'current');
      await observeFreshness(bridge, await readServedRuntimeConfig(context));
      await poll(() => bridge.evaluate(() => globalThis.catchupStates.length > 0), true, 12_000);
      const before = structuredClone(platform.requestCounts);
      await page.close();
      await terminateExtensionWorker(context, worker);
      const now = new Date().toISOString();
      platform.seedCatchup('101', 'missing101', now);
      platform.seedCatchup('102', 'missing102', now);
      platform.seedCatchup('103', 'missing103', now);
      page = await reopen();
      if (enabled) {
        await poll(async () => (await summary()).messageCount, 5, 240_000);
        await poll(async () => bridge.evaluate(() => {
          const states = globalThis.catchupStates;
          const behind = states.indexOf('behind');
          const checking = states.indexOf('checking', behind + 1);
          return behind >= 0 && checking > behind && states.indexOf('current', checking + 1) > checking;
        }), true);
        expect(platform.requestCounts.catchup_list - before.catchup_list).toBe(Math.ceil(3 / 100));
        expect(platform.requestCounts.catchup_messages - before.catchup_messages).toBe(3);
        for (const chat of ['101', '102', '103']) {
          const ids = await bridge.evaluate(async chat => (await (await fetch(
            '/api/v1/conversations/' + chat + '/messages?limit=100')).json()).items.map(item => item.message_id), chat);
          expect(ids).toContain('missing' + chat);
        }
      } else {
        await poll(async () => (await summary()).agentStatus, 'connected', 90_000);
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
