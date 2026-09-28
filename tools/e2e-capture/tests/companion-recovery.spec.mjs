import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
} from '../../../extension/transport/provisioning-identity.mjs';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BrainProcess } from '../lib/brain.mjs';
import { establishBrowserWebAuthnSession, readBrainSummary, requestAgentPairingTicket } from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import {
  bindAgentFromBridgePage,
  extensionId,
  extensionState,
  extensionWorker,
  launchExtensionBrowser,
  terminateExtensionWorker,
} from '../lib/extension-browser.mjs';
import { assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForAdmission(context, worker, previousToken = null, timeoutMs = 90_000) {
  await expect.poll(async () => {
    const state = await extensionState(worker);
    const summary = await readBrainSummary(context);
    return state.socketOpen && state.sessionBound && summary.agentStatus === 'connected'
      && summary.connectionToken !== null && summary.connectionToken !== previousToken;
  }, { timeout: timeoutMs, message: 'The replacement worker did not gain a new admitted session.' }).toBe(true);
  return readBrainSummary(context);
}

test('companion recovers after worker stops and seven stable restart cycles', async () => {
  test.setTimeout(1_100_000);
  assertBuiltSpa();
  assertBuiltExtension();
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'companion-recovery-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  const authDatabasePath = path.join(temporaryRoot, 'auth.sqlite3');
  let context = null;
  let brain = null;
  try {
    context = await launchExtensionBrowser(browserProfile);
    let worker = await extensionWorker(context);
    const actualExtensionId = extensionId(worker);
    brain = new BrainProcess({
      authDatabasePath,
      canonicalDatabasePath: path.join(temporaryRoot, 'canonical.sqlite3'),
      projectionDatabasePath: path.join(temporaryRoot, 'projections.sqlite3'),
      extensionId: actualExtensionId,
    });
    await brain.start();

    const pageErrors = [];
    const popup = await openPopup(context, actualExtensionId, pageErrors);
    await connectFullAnalytics(context, popup, worker);
    await popup.close();
    const bindingPage = context.pages()[0] ?? await context.newPage();
    await establishBrowserWebAuthnSession(bindingPage, authDatabasePath);
    const pairing = await requestAgentPairingTicket(context);
    await bindAgentFromBridgePage(bindingPage, {
      extensionId: actualExtensionId,
      creatorAccountId: pairing.creatorAccountId,
      authTicket: pairing.pairingTicket,
      storageBootstrap: pairing.storageBootstrap,
    });

    const platform = new SyntheticPlatform();
    await platform.install(context);
    const platformPage = await context.newPage();
    platformPage.on('pageerror', (error) => pageErrors.push(error.message));
    await platformPage.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded' });
    await expect.poll(() => platformPage.evaluate(
      () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
    )).toBe('full');
    await platformPage.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
    await expect.poll(() => worker.evaluate(async ({ key, schema }) => {
      const saved = (await chrome.storage.session.get([key]))[key];
      return saved?.schema === schema && saved.contexts?.some(
        (entry) => entry.observed_platform_id === 'dev-creator-account',
      );
    }, { key: PROVISIONING_IDENTITY_STORAGE_KEY, schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA })).toBe(true);
    await platformPage.evaluate(() => globalThis.fixtureRead('/api2/v2/chats'));
    await platformPage.evaluate((pathname) => globalThis.fixtureRead(pathname),
      `/api2/v2/chats/${SYNTHETIC.chatId}/messages`);
    await platformPage.evaluate(() => globalThis.fixtureOpenSocket());
    await expect.poll(async () => (await extensionState(worker)).outbox?.acknowledgedSourceSeq).toBe(4);
    let admitted = await waitForAdmission(context, worker);

    async function restartAfterStableSession() {
      await sleep(12_000);
      const previous = worker;
      const previousToken = admitted.connectionToken;
      const deadline = Date.now() + 90_000;
      const stopped = await terminateExtensionWorker(context, previous);
      try {
        expect(stopped.stoppedNormally).toBe(true);
        expect(stopped.stopMethod).toBe('stopWorker');
        worker = await extensionWorker(context, {
          differentFrom: previous, timeoutMs: Math.max(1, deadline - Date.now()),
        });
        expect(worker).not.toBe(previous);
        admitted = await waitForAdmission(context, worker, previousToken,
          Math.max(1, deadline - Date.now()));
        const circuit = await worker.evaluate(async () => (
          (await chrome.storage.local.get(['companion_recovery_v1'])).companion_recovery_v1
        ));
        expect(circuit.attempts).toBeLessThan(6);
        expect(circuit.next_attempt_at - Date.now()).toBeLessThan(90_000);
      } finally {
        await stopped.releaseControlPage();
      }
    }

    await restartAfterStableSession();
    platform.sendInitialMessageOnlyPeer();
    await expect.poll(async () => (await extensionState(worker)).outbox?.acknowledgedSourceSeq,
      { timeout: 30_000 }).toBe(6);
    await expect.poll(async () => (await readBrainSummary(context)).messageCount,
      { timeout: 30_000 }).toBe(4);

    for (let cycle = 0; cycle < 7; cycle += 1) await restartAfterStableSession();
    expect(pageErrors).toEqual([]);
    platform.assertFailClosed();
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
