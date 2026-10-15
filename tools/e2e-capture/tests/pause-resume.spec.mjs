import { mkdtemp, rm } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BOOTSTRAP_CONFIG_REVISION, BrainProcess } from '../lib/brain.mjs';
import { establishBrowserWebAuthnSession, readBrainSummary, pairCompanionForCapture } from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import { extensionId, extensionState, extensionWorker,
  launchExtensionBrowser } from '../lib/extension-browser.mjs';
import { assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';
import { withCatchupDiagnostics } from '../lib/catchup-diagnostics.mjs';

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

async function setCaptureFromBridge(page, action) {
  const result = await page.evaluate(async (captureAction) => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!csrf) throw new Error('Bridge CSRF token is unavailable');
    const response = await fetch('/api/v1/companion/browser/capture', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ action: captureAction }),
    });
    return { status: response.status, body: await response.json().catch(() => null) };
  }, action);
  const reason = ['browser_unreachable', 'pairing_account_refused', 'pairing_state_refused',
    'pairing_storage_refused'].includes(result.body?.detail) ? result.body.detail : 'unavailable';
  expect(result.status, `Desktop capture delivery (${reason})`).toBe(202);
  expect(result.body?.delivered).toBeGreaterThan(0);
}

async function admitted(context, worker, previousToken = null) {
  let summary;
  await expect.poll(async () => {
    const state = await extensionState(worker);
    summary = await readBrainSummary(context);
    return state.capturePhase === 'full' && state.socketOpen && state.sessionBound
      && state.syncRequired === false && state.appliedConfigRevision === BOOTSTRAP_CONFIG_REVISION
      && state.enabledResources.includes('chats') && state.enabledResources.includes('messages')
      && summary.agentStatus === 'connected'
      && summary.connectionToken !== null && summary.connectionToken !== previousToken;
  }, { timeout: 90_000,
    message: previousToken === null ? 'wait for initial agent admission' : 'wait for resumed agent admission',
  }).toBe(true);
  return summary;
}

async function captureDocumentReady(worker, documentToken) {
  return worker.evaluate(async token => {
    const tabs = await chrome.tabs.query({ url: ['https://onlyfans.com/*'] });
    for (const tab of tabs) {
      if (!Number.isInteger(tab.id)) continue;
      let document;
      try {
        [document] = await chrome.scripting.executeScript({
          target: { tabId: tab.id, frameIds: [0] }, world: 'MAIN',
          func: () => globalThis.fixtureDocumentToken ?? null,
        });
      } catch { continue; }
      if (document?.result !== token) continue;
      try {
        const status = await chrome.tabs.sendMessage(tab.id, {
          type: 'ofca.capture.control', version: 1, action: 'status',
        }, { frameId: 0 });
        // The isolated bridge combines its consent admission with MAIN status.
        // A configured MAIN hook alone can still be waiting for that response.
        return { ready: status?.mode === 'full' && status.active === true && status.forwarding === true,
          mode: ['identity', 'preview', 'full'].includes(status?.mode) ? status.mode : null,
          active: typeof status?.active === 'boolean' ? status.active : null,
          forwarding: typeof status?.forwarding === 'boolean' ? status.forwarding : null,
          stage: 'status' };
      } catch { return { ready: false, stage: 'status_failed' }; }
    }
    return { ready: false, stage: 'document_missing' };
  }, documentToken);
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
    await pairCompanionForCapture(context);
    const platform = new SyntheticPlatform();
    await platform.install(context);
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded', timeout: 15_000 });
    const documentToken = await page.evaluate(() => globalThis.fixtureDocumentToken);
    let captureAdmission = null;
    try {
      await expect.poll(async () => {
        captureAdmission = await captureDocumentReady(worker, documentToken);
        return captureAdmission.ready;
      }, { timeout: 12_000, message: 'wait for Full capture in both page worlds' }).toBe(true);
    } catch (error) {
      console.error('capture-page-admission: ' + JSON.stringify(captureAdmission));
      throw error;
    }
    await page.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
    const before = await admitted(context, worker);
    await page.evaluate(() => globalThis.fixtureRead('/api2/v2/chats'));
    await page.evaluate((pathname) => globalThis.fixtureRead(pathname), `/api2/v2/chats/${SYNTHETIC.chatId}/messages`);
    await page.evaluate(() => globalThis.fixtureOpenSocket());
    let initialSummary = null;
    await withCatchupDiagnostics(() => expect.poll(async () => {
      initialSummary = await readBrainSummary(context);
      return initialSummary.messageCount;
    }, { timeout: 30_000, message: 'wait for initial message count' }).toBe(3), {
      summary: () => initialSummary ?? readBrainSummary(context), platform, context, brain, failureContext: 'capture',
    });
    const previousInstance = (await extensionState(worker)).workerInstanceId;
    expect(previousInstance).not.toBeNull();
    await expect.poll(async () => (await readBrainSummary(context)).browser?.capture ?? null,
      { timeout: 30_000, message: 'wait for active browser control state' }).toBe('active');
    let controls = await openPopup(context, id, errors);
    await expect(controls.locator('#pause'), 'desktop-owned Pause is hidden').toBeHidden();
    const capture = action => withCatchupDiagnostics(() => setCaptureFromBridge(binding, action), {
      summary: () => readBrainSummary(context), platform, context, brain, failureContext: 'capture',
    });
    await capture('pause');
    await expect(controls.locator('#journey-primary'), 'wait for desktop entry after pause')
      .toHaveText('Continue in the desktop app', { timeout: 8_000 });
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
    await expect(controls.locator('#journey-primary'), 'wait for desktop entry after worker replacement')
      .toHaveText('Continue in the desktop app', { timeout: 8_000 });
    expect(await popupConsent(controls, 'read paused consent after worker replacement'))
      .toEqual({ mode: 'paused', reload: false });
    expect((await readBrainSummary(context)).messageCount).toBe(3);
    await expect.poll(async () => (await readBrainSummary(context)).browser?.capture ?? null,
      { timeout: 30_000, message: 'wait for paused browser control state' }).toBe('paused');
    await capture('resume');
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
