import { mkdtemp, rm } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BrainProcess } from '../lib/brain.mjs';
import { establishBrowserWebAuthnSession, readBrainSummary, requestAgentPairingTicket } from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import { bindAgentFromBridgePage, extensionId, extensionState, extensionWorker,
  launchExtensionBrowser } from '../lib/extension-browser.mjs';
import { assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function clickPopup(popup, selector, step) {
  try {
    await popup.locator(selector).click({ timeout: 8_000 });
  } catch (error) {
    throw new Error(`${step}: popup click failed`, { cause: error });
  }
}

async function popupConsent(popup, step) {
  return popup.evaluate(async (stepName) => {
    let timer;
    try {
      const reply = await Promise.race([
        chrome.runtime.sendMessage({ type: 'ofca.ui.status' }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`${stepName}: status request timed out`)), 6_000);
        }),
      ]);
      if (reply?.ok !== true) throw new Error(`${stepName}: status request failed`);
      return { mode: reply.status.consent.mode, reload: reply.status.reload_required };
    } catch (error) {
      throw new Error(`${stepName}: popup status read failed`, { cause: error });
    } finally {
      clearTimeout(timer);
    }
  }, step);
}

async function admitted(context, worker, previousToken = null) {
  let summary;
  await expect.poll(async () => {
    const state = await extensionState(worker);
    summary = await readBrainSummary(context);
    return state.socketOpen && state.sessionBound && summary.agentStatus === 'connected'
      && summary.connectionToken !== null && summary.connectionToken !== previousToken;
  }, { timeout: 90_000,
    message: previousToken === null ? 'wait for initial agent admission' : 'wait for resumed agent admission',
  }).toBe(true);
  return summary;
}

async function probeDelivery(page) {
  return page.evaluate(async (chatId) => {
    const response = await fetch(`/api/v1/conversations/${encodeURIComponent(chatId)}/messages?limit=100`, {
      credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store',
    });
    if (!response.ok) throw new Error('Unable to read the synthetic conversation');
    const { items } = await response.json();
    return { paused: items.some((item) => item.message_id === 'paused-probe'),
      resumed: items.some((item) => item.message_id === 'resumed-probe'), count: items.length };
  }, SYNTHETIC.chatId);
}

async function stopAndObserveReplacement(context, page, worker) {
  const marker = randomUUID();
  await worker.evaluate((value) => { globalThis.__PAUSE_TEST_REALM__ = value; }, marker);
  const cdp = await context.newCDPSession(page);
  try {
    const versions = new Map();
    cdp.on('ServiceWorker.workerVersionUpdated', ({ versions: updates }) => {
      for (const version of updates) versions.set(version.versionId, version);
    });
    await cdp.send('ServiceWorker.enable');
    let running;
    await expect.poll(() => {
      running = [...versions.values()].find((version) => (
        version.scriptURL === worker.url() && version.runningStatus === 'running'
      ));
      return Boolean(running);
    }, { timeout: 12_000, message: 'wait for running worker version' }).toBe(true);
    await cdp.send('ServiceWorker.stopWorker', { versionId: running.versionId });
    let replacement = null;
    await expect.poll(async () => {
      for (const candidate of context.serviceWorkers().filter((entry) => entry.url() === worker.url())) {
        const fresh = await Promise.race([
          candidate.evaluate(() => (
            globalThis.__PAUSE_TEST_REALM__ === undefined
            && typeof globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__ === 'function'
          )).catch(() => false),
          delay(500).then(() => false),
        ]);
        if (fresh) { replacement = candidate; return true; }
      }
      return false;
    }, { timeout: 120_000, message: 'wait for replacement worker realm' }).toBe(true);
    return replacement;
  } finally {
    await cdp.detach();
  }
}

test('soft pause survives worker replacement and resumes the existing socket without reload', async () => {
  test.setTimeout(600_000);
  assertBuiltSpa(); assertBuiltExtension();
  const root = await mkdtemp(path.join(tmpdir(), 'pause-resume-'));
  let context;
  let brain;
  try {
    context = await launchExtensionBrowser(path.join(root, 'profile'));
    let worker = await extensionWorker(context, { timeoutMs: 15_000 });
    const id = extensionId(worker);
    const authDatabasePath = path.join(root, 'auth.sqlite3');
    brain = new BrainProcess({ authDatabasePath, canonicalDatabasePath: path.join(root, 'canonical.sqlite3'),
      projectionDatabasePath: path.join(root, 'projections.sqlite3'), extensionId: id });
    await brain.start();
    const errors = [];
    const popup = await openPopup(context, id, errors);
    await connectFullAnalytics(context, popup, worker);
    for (const page of context.pages()) {
      if (page.url().startsWith('chrome-extension://')) await page.close();
    }
    const binding = context.pages()[0] ?? await context.newPage();
    await establishBrowserWebAuthnSession(binding, authDatabasePath);
    const pairing = await requestAgentPairingTicket(context);
    await bindAgentFromBridgePage(binding, { extensionId: id, creatorAccountId: pairing.creatorAccountId,
      authTicket: pairing.pairingTicket, storageBootstrap: pairing.storageBootstrap });
    const platform = new SyntheticPlatform();
    await platform.install(context);
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded', timeout: 15_000 });
    await expect.poll(() => page.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode),
      { timeout: 12_000, message: 'wait for Full page hook' }).toBe('full');
    await page.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
    const before = await admitted(context, worker);
    await page.evaluate(() => globalThis.fixtureRead('/api2/v2/chats'));
    await page.evaluate((pathname) => globalThis.fixtureRead(pathname), `/api2/v2/chats/${SYNTHETIC.chatId}/messages`);
    await page.evaluate(() => globalThis.fixtureOpenSocket());
    await expect.poll(async () => (await readBrainSummary(context)).messageCount,
      { timeout: 30_000, message: 'wait for initial message count' }).toBe(3);
    const documentToken = await page.evaluate(() => globalThis.fixtureDocumentToken);
    const previousInstance = (await extensionState(worker)).workerInstanceId;
    expect(previousInstance).not.toBeNull();
    let controls = await openPopup(context, id, errors);
    await clickPopup(controls, '#pause', 'click Pause analytics');
    await expect(controls.locator('#journey-primary'), 'wait for Resume analytics after pause')
      .toHaveText('Resume analytics', { timeout: 8_000 });
    expect(await popupConsent(controls, 'read paused consent')).toEqual({ mode: 'paused', reload: false });
    platform.sendPauseProbe('paused-probe');
    await expect.poll(() => page.evaluate(() => globalThis.fixtureSocketFrames),
      { timeout: 12_000, message: 'wait for paused socket frame' }).toBe(1);
    expect((await readBrainSummary(context)).messageCount).toBe(3);
    expect(await probeDelivery(binding)).toEqual({ paused: false, resumed: false, count: 3 });

    await controls.close();
    worker = await stopAndObserveReplacement(context, page, worker);
    controls = await openPopup(context, id, errors);
    await expect(controls.locator('main'), 'wait for popup after worker replacement')
      .toHaveAttribute('data-ready', 'true', { timeout: 8_000 });
    await expect(controls.locator('#journey-primary'), 'wait for Resume analytics after worker replacement')
      .toHaveText('Resume analytics', { timeout: 8_000 });
    expect(await popupConsent(controls, 'read paused consent after worker replacement'))
      .toEqual({ mode: 'paused', reload: false });
    expect((await readBrainSummary(context)).messageCount).toBe(3);
    await clickPopup(controls, '#journey-primary', 'click Resume analytics');
    await expect.poll(() => popupConsent(controls, 'read resumed consent'),
      { timeout: 8_000, message: 'wait for Full consent after resume' })
      .toEqual({ mode: 'full', reload: false });
    await admitted(context, worker, before.connectionToken);
    expect((await extensionState(worker)).workerInstanceId).not.toBe(previousInstance);
    expect((await extensionState(worker)).workerInstanceId).not.toBeNull();
    platform.sendPauseProbe('resumed-probe');
    await expect.poll(async () => (await readBrainSummary(context)).messageCount,
      { timeout: 30_000, message: 'wait for resumed message count' }).toBe(4);
    await expect.poll(async () => (await extensionState(worker)).outbox?.pendingEntries,
      { timeout: 12_000, message: 'wait for empty outbox' }).toBe(0);
    await expect.poll(() => probeDelivery(binding),
      { timeout: 30_000, message: 'wait for delivered resumed record' })
      .toEqual({ paused: false, resumed: true, count: 4 });
    expect((await readBrainSummary(context)).messageCount).toBe(4);
    expect(await popupConsent(controls, 'read final Full consent')).toEqual({ mode: 'full', reload: false });
    expect(await page.evaluate(() => globalThis.fixtureDocumentToken)).toBe(documentToken);
    expect(await page.evaluate(() => globalThis.fixtureSocketFrames)).toBe(2);
    expect(errors).toEqual([]);
    platform.assertFailClosed();
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
});
