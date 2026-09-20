import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
} from '../../../extension/transport/provisioning-identity.mjs';
import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BOOTSTRAP_CONFIG_REVISION, BRAIN_ORIGIN, BrainProcess } from '../lib/brain.mjs';
import {
  establishBrowserWebAuthnSession,
  readBrainSummary,
  requestAgentPairingTicket,
} from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import {
  bindAgentFromBridgePage,
  contentBridgeIsActive,
  extensionId,
  extensionOutboxProof,
  extensionState,
  extensionWorker,
  extensionWorkerTargetCount,
  launchExtensionBrowser,
  restartedExtensionWorker,
  startExtensionWorker,
  terminateExtensionWorker,
} from '../lib/extension-browser.mjs';
import { assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';
import { readSqliteProof } from '../lib/sqlite-proof.mjs';

const IDENTITY_PATH = '/api2/v2/users/me';
const CHATS_PATH = '/api2/v2/chats';
const MESSAGES_PATH = `/api2/v2/chats/${SYNTHETIC.chatId}/messages`;
const WORKER_RECOVERY_TIMEOUT_MS = 90_000;
const SAFE_ALARM_REMAINING_MS = 12_000;

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function watchExtensionWorkers(context) {
  const creations = [];
  const listener = (worker) => {
    if (
      worker.url().startsWith('chrome-extension://')
      && worker.url().endsWith('/background.js')
    ) creations.push({ worker, createdAt: Date.now() });
  };
  context.on('serviceworker', listener);
  return {
    creations,
    stop() { context.off('serviceworker', listener); },
  };
}

async function waitForExtensionState(
  worker,
  predicate,
  message,
  { timeoutMs = 20_000 } = {},
) {
  let latest = null;
  try {
    await expect.poll(async () => {
      latest = await extensionState(worker);
      return predicate(latest);
    }, { message, timeout: timeoutMs }).toBe(true);
  } catch (error) {
    let identityContext = null;
    let identityReadError = null;
    try {
      identityContext = await worker.evaluate(async (storageKey) => {
        const stored = await chrome.storage.session.get([storageKey]);
        return stored[storageKey] ?? null;
      }, PROVISIONING_IDENTITY_STORAGE_KEY);
    } catch (identityError) {
      identityReadError = identityError instanceof Error
        ? identityError.message
        : String(identityError);
    }
    const identityDiagnostic = identityReadError === null
      ? `Provisioning identity context: ${JSON.stringify(identityContext)}`
      : `Provisioning identity context read failed: ${identityReadError}`;
    throw new Error(
      `${message}\nLast extension state: ${JSON.stringify(latest)}\n${identityDiagnostic}`,
      { cause: error },
    );
  }
  return latest;
}

async function waitForBrain(context, predicate, message, { timeoutMs = 25_000 } = {}) {
  let latest = null;
  let lastError = null;
  try {
    await expect.poll(async () => {
      try {
        latest = await readBrainSummary(context);
        return predicate(latest);
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
        return false;
      }
    }, { message, timeout: timeoutMs }).toBe(true);
  } catch (error) {
    const diagnostic = lastError === null
      ? `Last Brain summary: ${JSON.stringify(latest)}`
      : `Last Brain probe error: ${lastError}`;
    throw new Error(`${message}\n${diagnostic}`, { cause: error });
  }
  return latest;
}

async function waitForSafeAlarmWindow(worker) {
  // The independently scheduled retention alarm also legitimately wakes MV3.
  // Isolate the reconciliation alarm for this specific recovery proof.
  await worker.evaluate(() => chrome.alarms.clear('ofca-preview-retention'));
  let latest = null;
  await expect.poll(async () => {
    latest = await extensionState(worker);
    return (latest.reconcileAlarm?.scheduledTime ?? 0) - Date.now();
  }, {
    message: 'The production reconciliation alarm did not expose a safe hard-expiry window.',
    timeout: 75_000,
  }).toBeGreaterThan(SAFE_ALARM_REMAINING_MS);
  expect(latest.reconcileAlarm.name).toBe('ofca-agent-reconcile');
  expect(latest.reconcileAlarm.periodInMinutes).toBe(1);
  return latest.reconcileAlarm;
}

async function readPlatform(page, pathname) {
  await page.evaluate(async (pathValue) => globalThis.fixtureRead(pathValue), pathname);
}

async function waitForObservedCreatorIdentity(worker, documentToken, accountId) {
  await expect.poll(async () => worker.evaluate(async ({
    account,
    storageKey,
    storageSchema,
    token,
  }) => {
    const tabs = await chrome.tabs.query({ url: ['https://onlyfans.com/*'] });
    let tabId = null;
    for (const tab of tabs) {
      if (!Number.isInteger(tab.id)) continue;
      try {
        const [result] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: 'MAIN',
          func: () => globalThis.fixtureDocumentToken ?? null,
        });
        if (result?.result === token) {
          tabId = tab.id;
          break;
        }
      } catch (_error) {
        // Non-fixture OnlyFans tabs cannot satisfy this identity fence.
      }
    }
    if (tabId === null) return false;
    const stored = await chrome.storage.session.get([storageKey]);
    const document = stored[storageKey];
    return document?.schema === storageSchema
      && Array.isArray(document.contexts)
      && document.contexts.some((context) => (
        context?.tab_id === tabId
        && context?.observed_platform_id === account
        && typeof context?.page_epoch === 'string'
      ));
  }, {
    account: accountId,
    storageKey: PROVISIONING_IDENTITY_STORAGE_KEY,
    storageSchema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    token: documentToken,
  }), {
    message: 'The active capture document did not persist its observed creator identity.',
    timeout: 10_000,
  }).toBe(true);
}

function expectAlarmCreatedTarget(restart, scheduledTime) {
  expect(restart.createdAt).toBeGreaterThanOrEqual(scheduledTime - 1_500);
}

function expectNoDrops(state) {
  expect(Object.values(state.drops).reduce((sum, count) => sum + count, 0)).toBe(0);
}

function expectStablePersistenceProof(after, before) {
  expect(after).toEqual(before);
}

test('real MV3 capture proves exact ordering, durable replay, and alarm recovery', async () => {
  // Repeated intentional worker termination can now open the real five-minute
  // recovery circuit. Observe its expiry instead of bypassing persisted state.
  test.setTimeout(600_000);
  test.slow();
  assertBuiltSpa();
  assertBuiltExtension();
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-e2e-capture-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  const canonicalDatabasePath = path.join(temporaryRoot, 'canonical.sqlite3');
  const authDatabasePath = path.join(temporaryRoot, 'auth.sqlite3');
  const projectionDatabasePath = path.join(temporaryRoot, 'projections.sqlite3');
  const databasePaths = { canonicalDatabasePath, projectionDatabasePath };
  let brain = null;
  let context = null;
  let worker = null;
  const pageErrors = [];

  try {
    await test.step('load the audited MV3 artifact and pair it through the exact Bridge origin', async () => {
      context = await launchExtensionBrowser(browserProfile);
      worker = await extensionWorker(context);
      const actualExtensionId = extensionId(worker);
      brain = new BrainProcess({
        authDatabasePath,
        ...databasePaths,
        extensionId: actualExtensionId,
      });
      await brain.start();

      const popup = await openPopup(context, actualExtensionId, pageErrors);
      await connectFullAnalytics(context, popup, worker);
      await expect.poll(async () => (await extensionState(worker)).capturePhase, {
        message: 'Granting both origins did not move capture into the identity phase.',
      }).toBe('identity');
      await popup.close();

      const bindingPage = context.pages()[0] ?? await context.newPage();
      await establishBrowserWebAuthnSession(bindingPage, authDatabasePath);
      const provisioningIdentity = await bindingPage.evaluate(async (targetExtensionId) => (
        Promise.race([
          chrome.runtime.sendMessage(targetExtensionId, {
            type: 'provisioning.identity.query', version: 1,
          }),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error('The unpaired worker blocked the desktop identity query.')),
            3_000,
          )),
        ])
      ), actualExtensionId);
      expect(provisioningIdentity).toEqual({
        type: 'provisioning.identity.result',
        version: 1,
        authenticated_profile: null,
      });
      const pairing = await requestAgentPairingTicket(context);
      expect(pairing.creatorAccountId).toBe('dev-creator-account');
      expect(pairing.extensionId).toBe(actualExtensionId);
      await bindAgentFromBridgePage(bindingPage, {
        extensionId: actualExtensionId,
        creatorAccountId: pairing.creatorAccountId,
        authTicket: pairing.pairingTicket,
        storageBootstrap: pairing.storageBootstrap,
      });

      await expect.poll(async () => (await extensionState(worker)).capturePhase, {
        message: 'Pairing did not reconcile the capture phase to full.',
      }).toBe('full');
    });

    const platform = new SyntheticPlatform();
    await platform.install(context);
    const platformPage = context.pages()[0] ?? await context.newPage();
    platformPage.on('pageerror', (error) => pageErrors.push(error.message));
    await platformPage.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded' });
    const platformDocumentToken = await platformPage.evaluate(
      () => globalThis.fixtureDocumentToken,
    );

    await test.step('prove both page worlds and Brain-owned capture policy are active', async () => {
      await expect.poll(
        () => platformPage.evaluate(
          () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
        ),
        { message: 'The MAIN-world page hook did not install in full capture mode.' },
      ).toBe('full');
      await expect.poll(() => contentBridgeIsActive(worker)).toBe(true);
      await readPlatform(platformPage, IDENTITY_PATH);
      await waitForObservedCreatorIdentity(worker, platformDocumentToken, SYNTHETIC.creatorId);
      const state = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.runtimeReady
          && candidate.socketOpen
          && candidate.sessionBound
          && candidate.heartbeatTimerPresent
          && candidate.syncRequired === false
          && candidate.appliedConfigRevision === BOOTSTRAP_CONFIG_REVISION
          && candidate.enabledResources.includes('chats')
          && candidate.enabledResources.includes('messages')
          && candidate.reconcileAlarm?.periodInMinutes === 1
        ),
        `Agent did not bind and apply the ${BOOTSTRAP_CONFIG_REVISION} capture policy.`,
      );
      expect(state.outbox).not.toBeNull();
      expect(state.outbox.lastSourceSeq).toBe(0);
      expect(state.outbox.acknowledgedSourceSeq).toBe(0);
      expect(state.outbox.pendingEntries).toBe(0);
    });

    await test.step('SPA navigation fences identity without interrupting the authenticated connection', async () => {
      const before = await readBrainSummary(context);
      await platformPage.evaluate(() => {
        globalThis.fixtureIdentityResets = 0;
        window.addEventListener('message', (event) => {
          if (event.data?.type === 'ofca.provisioning.identity.reset') globalThis.fixtureIdentityResets += 1;
        });
      });
      for (let navigation = 0; navigation < 5; navigation += 1) {
        await platformPage.evaluate((index) => history.pushState(null, '', `/my/chats?recovery=${index}`), navigation);
        await expect.poll(async () => ({
          resets: await platformPage.evaluate(() => globalThis.fixtureIdentityResets),
          unknown: await worker.evaluate(async (key) => {
          const saved = await chrome.storage.session.get([key]);
          const tab = (await chrome.tabs.query({ url: ['https://onlyfans.com/my/chats?recovery=*'] }))[0];
          return saved[key]?.contexts?.filter((entry) => entry.tab_id === tab?.id)
            .every((entry) => entry.observed_platform_id === null);
          }, PROVISIONING_IDENTITY_STORAGE_KEY),
        })).toEqual({ resets: navigation + 1, unknown: true });
        await readPlatform(platformPage, IDENTITY_PATH);
        await waitForObservedCreatorIdentity(worker, platformDocumentToken, SYNTHETIC.creatorId);
        expect((await readBrainSummary(context)).connectionToken).toBe(before.connectionToken);
      }
    });

    await test.step('produce exactly one chat and four message observations as sequence 1-6', async () => {
      await readPlatform(platformPage, CHATS_PATH);
      await readPlatform(platformPage, MESSAGES_PATH);

      await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.outbox?.chatCount === 1
          && candidate.outbox?.messageCount === 3
          && candidate.outbox?.lastSourceSeq === 4
          && candidate.outbox?.acknowledgedSourceSeq === 4
          && candidate.outbox?.pendingEntries === 0
        ),
        'The explicit chat plus three wrapped history messages did not acknowledge as 1-4.',
      );

      await platformPage.evaluate(() => globalThis.fixtureOpenSocket());
      await expect.poll(() => platform.openSockets.size).toBeGreaterThan(0);
      platform.sendInitialMessageOnlyPeer();

      const state = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.outbox?.chatCount === 2
          && candidate.outbox?.messageCount === 4
          && candidate.outbox?.lastSourceSeq === 6
          && candidate.outbox?.acknowledgedSourceSeq === 6
          && candidate.outbox?.pendingEntries === 0
        ),
        'The message-only peer did not atomically acknowledge parent/message sequences 5-6.',
      );
      expectNoDrops(state);
      expect(platform.websocketFramesSent).toBe(1);
    });

    let initialConnection;
    const bridgeMessageTraffic = [];
    const bridgePage = await test.step('prove exact Brain, SQLite, heartbeat, and Inbox state', async () => {
      const first = await waitForBrain(
        context,
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.appliedConfigRevision === candidate.requiredConfigRevision
          && candidate.conversationCount === 2
          && candidate.messageCount === 4
          && candidate.analyticsBasis === 'synced_subset'
          && candidate.summaryOnly === true
          && candidate.viewRevision > 0
        ),
        'Brain did not project the exact initial two-conversation/four-message state.',
      );
      initialConnection = first.connectionToken;

      await sleep(2_200);
      const second = await waitForBrain(
        context,
        (candidate) => (
          candidate.agentStatus === 'connected'
          && Date.parse(candidate.lastHeartbeatAt) > Date.parse(first.lastHeartbeatAt)
          && candidate.connectionToken === first.connectionToken
        ),
        'Agent heartbeat did not advance on the bound connection.',
      );
      expect(second.connectionToken).toBe(initialConnection);

      const proof = await readSqliteProof(databasePaths);
      expect(proof.streamCount).toBe(1);
      expect(proof.committedSourceSeq).toBe(6);
      expect(proof.eventCount).toBe(6);
      expect(proof.eventSequences).toEqual([1, 2, 3, 4, 5, 6]);
      expect(proof.eventChangeTypes).toEqual([
        'chat.upsert',
        'message.upsert',
        'message.upsert',
        'message.upsert',
        'chat.upsert',
        'message.upsert',
      ]);
      expect(proof.eventSequenceIsContiguous).toBe(true);
      expect(proof.canonicalChatCount).toBe(2);
      expect(proof.canonicalMessageCount).toBe(4);
      expect(proof.readModelChatCount).toBe(2);
      expect(proof.readModelMessageCount).toBe(4);
      expect(proof.messageAnalysisCount).toBe(4);
      expect(proof.lpgNodeCount).toBe(6);
      expect(proof.lpgEdgeCount).toBe(4);
      expect([1, 2]).toContain(proof.inactiveReadModelChatCount);
      expect(proof.inactiveReadModelMessageCount).toBe(3);
      expect(proof.inactiveMessageAnalysisCount).toBe(3);
      expect(proof.inactiveLpgNodeCount).toBe(
        proof.inactiveReadModelChatCount + proof.inactiveReadModelMessageCount,
      );
      expect(proof.inactiveLpgEdgeCount).toBe(3);
      expect(proof.inactiveReadModelMessageCount).toBeLessThan(proof.readModelMessageCount);
      expect(proof.projectionSlotCount).toBe(2);
      expect(proof.maximumProjectionSlotsPerAccount).toBe(2);
      expect(proof.projectionSlotCountsByAccount).toEqual([
        { creatorAccountId: 'dev-creator-account', slotCount: 2 },
      ]);
      expect(proof.activeProjectionSlots).toHaveLength(1);
      expect(proof.inactiveProjectionSlots).toHaveLength(1);
      expect(proof.activeProjectionSlots[0].projectionSlot)
        .not.toBe(proof.inactiveProjectionSlots[0].projectionSlot);
      expect(proof.activeProjectionSlots[0].generationId)
        .not.toBe(proof.inactiveProjectionSlots[0].generationId);
      expect(proof.canonicalAccountIds).toEqual(['dev-creator-account']);
      expect(proof.projectionAccountIds).toEqual(['dev-creator-account']);
      expect(proof.projectionReadRevision).toBe(proof.viewRevision);

      const page = await context.newPage();
      page.on('request', (request) => {
        const url = new URL(request.url());
        if (url.pathname.startsWith('/api/v1/conversations/') && url.pathname.endsWith('/messages')) {
          bridgeMessageTraffic.push({ kind: 'request', method: request.method(), path: url.pathname });
        }
      });
      page.on('response', (response) => {
        const url = new URL(response.url());
        if (url.pathname.startsWith('/api/v1/conversations/') && url.pathname.endsWith('/messages')) {
          bridgeMessageTraffic.push({ kind: 'response', path: url.pathname, status: response.status() });
        }
      });
      page.on('requestfailed', (request) => {
        const url = new URL(request.url());
        if (url.pathname.startsWith('/api/v1/conversations/') && url.pathname.endsWith('/messages')) {
          bridgeMessageTraffic.push({
            kind: 'failure',
            path: url.pathname,
            reason: request.failure()?.errorText ?? 'unknown',
          });
        }
      });
      await page.goto(`${BRAIN_ORIGIN}/`, { waitUntil: 'domcontentloaded' });
      const dashboard = page.getByRole('main');
      await expect(
        dashboard.getByRole('heading', { level: 1, name: 'Dashboard' }),
      ).toBeVisible();
      const overview = dashboard.getByRole('region', { name: 'Overview' });
      for (const [label, value] of [
        ['Conversations', '2+'],
        ['Messages', '4+'],
      ]) {
        await expect(
          overview.getByRole('group', { name: label }).getByText(value, { exact: true }),
        ).toBeVisible();
      }
      for (const [label, value] of [
        ['Received', '3+'],
        ['Sent', '1+'],
      ]) {
        const legend = overview.getByText(label, { exact: true }).locator('..');
        await expect(legend.getByText(value, { exact: true })).toBeVisible();
      }
      await dashboard.getByRole('button', { name: 'Details' }).click();
      const details = page.getByRole('dialog', { name: 'How these numbers are counted' });
      await expect(details.getByText('Messages synced so far', { exact: true })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(details).toBeHidden();
      await expect(dashboard.getByRole('textbox')).toHaveCount(0);
      await expect(dashboard.getByRole('button', { name: /export/i })).toHaveCount(0);
      await expect(dashboard.getByText(/revenue/i)).toHaveCount(0);
      await expect(dashboard.getByText(/spend/i)).toHaveCount(0);

      const firstPageResponsePromise = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return url.origin === BRAIN_ORIGIN
          && url.pathname.startsWith('/api/v1/conversations/')
          && url.pathname.endsWith('/messages');
      });
      await page.getByRole('link', { name: 'Inbox', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible();
      const firstPageResponse = await firstPageResponsePromise;
      expect(firstPageResponse.status()).toBe(200);
      const firstPage = await firstPageResponse.json();
      expect(firstPage.conversation_id).toBe(SYNTHETIC.messageOnlyPeerId);
      expect(firstPage.items.map((item) => item.message_id)).toEqual([
        SYNTHETIC.messageOnlyMessageId,
      ]);
      await expect(page.getByRole('main').getByRole('alert')).toBeVisible();
      const conversationRows = page.locator('[aria-label="Conversation list"] [role="button"]');
      await expect(conversationRows).toHaveCount(2);

      await expect(page.getByRole('article')).toHaveCount(1);
      await expect(page.getByText('No earlier messages on this computer', { exact: true })).toBeVisible();

      const primaryPageResponsePromise = page.waitForResponse((response) => (
        new URL(response.url()).pathname
          === `/api/v1/conversations/${SYNTHETIC.chatId}/messages`
      ));
      await page.getByRole('button', {
        name: new RegExp(`^Conversation with ${SYNTHETIC.displayName},`),
      }).click();
      const primaryPageResponse = await primaryPageResponsePromise;
      expect(primaryPageResponse.status()).toBe(200);
      const primaryPage = await primaryPageResponse.json();
      expect(primaryPage.items.map((item) => item.message_id)).toEqual(
        SYNTHETIC.historyMessageIds,
      );
      await expect(page.getByRole('article')).toHaveCount(3);
      await expect(page.getByText('No earlier messages on this computer', { exact: true })).toBeVisible();
      const primaryMessageRows = await page.getByRole('article').count();

      const messageOnlyRow = conversationRows.filter({
        hasText: SYNTHETIC.messageOnlyText,
      });
      await expect(messageOnlyRow).toHaveCount(1);
      await messageOnlyRow.click();
      await expect(page.getByRole('article')).toHaveCount(1);
      const messageOnlyRows = await page.getByRole('article').count();
      expect(primaryMessageRows + messageOnlyRows).toBe(4);
      return page;
    });

    await test.step('a stable connection resets persisted recovery history through normal UI polling', async () => {
      const recoveryPopup = await openPopup(context, extensionId(worker), pageErrors);
      try {
        await expect.poll(async () => {
          await recoveryPopup.evaluate(() => chrome.runtime.sendMessage({ type: 'ofca.ui.status' }));
          return worker.evaluate(async () => (await chrome.storage.local.get(['companion_recovery_v1']))
            .companion_recovery_v1?.attempts);
        }, { timeout: 75_000, intervals: [1_000] }).toBe(0);
      } finally { await recoveryPopup.close(); }
      expect((await readBrainSummary(context)).connectionToken).toBe(initialConnection);
    });

    let pendingEncryptedOutbox;
    await test.step('persist exact encrypted pending sequences 7-8 while Brain is unavailable', async () => {
      await brain.stop();
      await waitForExtensionState(
        worker,
        (candidate) => !candidate.socketOpen && !candidate.sessionBound,
        'Agent did not observe Brain shutdown.',
      );

      platform.sendOfflineMessageOnlyPeer();
      const pending = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.outbox?.chatCount === 3
          && candidate.outbox?.messageCount === 5
          && candidate.outbox?.lastSourceSeq === 8
          && candidate.outbox?.acknowledgedSourceSeq === 6
          && candidate.outbox?.pendingEntries === 2
        ),
        'The offline message-only peer did not remain as pending parent/message sequences 7-8.',
      );
      const outboxProof = await extensionOutboxProof(worker);
      expect(outboxProof.sequences).toEqual([7, 8]);
      expect(outboxProof.records).toHaveLength(2);
      expect(outboxProof.serialized).not.toContain('chat.upsert');
      expect(outboxProof.serialized).not.toContain('message.upsert');
      expect(outboxProof.serialized).not.toContain(SYNTHETIC.offlinePeerId);
      expect(outboxProof.serialized).not.toContain(SYNTHETIC.offlineMessageId);
      expect(outboxProof.serialized).not.toContain(SYNTHETIC.offlineText);
      pendingEncryptedOutbox = outboxProof;
      expectNoDrops(pending);
      expect(platform.websocketFramesSent).toBe(2);
    });

    await test.step('restart with Brain offline, prove ciphertext durability, then replay', async () => {
      const oldWorker = worker;
      const oldWorkerInstanceId = (await extensionState(oldWorker)).workerInstanceId;
      const watcher = watchExtensionWorkers(context);
      try {
        const terminated = await terminateExtensionWorker(context, oldWorker);
        expect(terminated.stoppedNormally).toBe(true);
        expect(terminated.stopMethod).toBe('stopWorker');
        await startExtensionWorker(context, terminated.extensionOrigin);
        const restart = await restartedExtensionWorker(context, {
          previousTargetId: terminated.targetId,
          timeoutMs: WORKER_RECOVERY_TIMEOUT_MS,
        });
        worker = restart.worker;
        expect(await extensionOutboxProof(worker)).toEqual(pendingEncryptedOutbox);
        await waitForExtensionState(
          worker,
          (candidate) => candidate.capturePhase === 'identity' && !candidate.runtimeReady,
          'The restarted worker did not reach identity while Brain was offline.',
        );
        await brain.start();
      } finally {
        watcher.stop();
      }

      const recovered = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.socketOpen
          && candidate.sessionBound
          && candidate.syncRequired === false
          && candidate.outbox?.lastSourceSeq === 8
          && candidate.outbox?.acknowledgedSourceSeq === 8
          && candidate.outbox?.pendingEntries === 0
        ),
        'CDP-restarted worker did not replay and acknowledge exact sequences 7-8.',
        { timeoutMs: 60_000 },
      );
      expect(recovered.heartbeatTimerPresent).toBe(true);
      expect(recovered.workerInstanceId).not.toBe(oldWorkerInstanceId);

      const replayed = await waitForBrain(
        context,
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== initialConnection
          && candidate.conversationCount === 3
          && candidate.messageCount === 5
        ),
        'Brain did not bind the replacement worker and expose the replayed offline peer.',
      );

      const proof = await readSqliteProof(databasePaths);
      expect(proof.committedSourceSeq).toBe(8);
      expect(proof.eventCount).toBe(8);
      expect(proof.eventSequences).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
      expect(proof.eventIds.slice(6)).toHaveLength(2);
      expect(new Set(proof.eventIds.slice(6)).size).toBe(2);
      expect(proof.eventChangeTypes.slice(6)).toEqual(['chat.upsert', 'message.upsert']);
      expect(proof.canonicalChatCount).toBe(3);
      expect(proof.canonicalMessageCount).toBe(5);
      expect(proof.readModelChatCount).toBe(3);
      expect(proof.readModelMessageCount).toBe(5);
      expect(proof.messageAnalysisCount).toBe(5);
      expect(proof.lpgNodeCount).toBe(8);
      expect(proof.lpgEdgeCount).toBe(5);
      expect([2, 3]).toContain(proof.inactiveReadModelChatCount);
      expect(proof.inactiveReadModelMessageCount).toBe(4);
      expect(proof.inactiveMessageAnalysisCount).toBe(4);
      expect(proof.inactiveLpgNodeCount).toBe(
        proof.inactiveReadModelChatCount + proof.inactiveReadModelMessageCount,
      );
      expect(proof.inactiveLpgEdgeCount).toBe(4);
      expect(proof.inactiveReadModelMessageCount).toBeLessThan(proof.readModelMessageCount);
      expect(proof.projectionSlotCount).toBe(2);
      expect(proof.maximumProjectionSlotsPerAccount).toBe(2);
      expect(proof.projectionSlotCountsByAccount).toEqual([
        { creatorAccountId: 'dev-creator-account', slotCount: 2 },
      ]);
      expect(proof.activeProjectionSlots).toHaveLength(1);
      expect(proof.inactiveProjectionSlots).toHaveLength(1);
      expect(proof.activeProjectionSlots[0].projectionSlot)
        .not.toBe(proof.inactiveProjectionSlots[0].projectionSlot);
      expect(proof.activeProjectionSlots[0].generationId)
        .not.toBe(proof.inactiveProjectionSlots[0].generationId);
      expect(proof.activeProjectionSlots[0].readRevision)
        .toBeGreaterThan(proof.inactiveProjectionSlots[0].readRevision);
      expect(proof.projectionReadRevision).toBe(proof.viewRevision);

      expect(replayed.conversationCount).toBe(proof.readModelChatCount);
      expect(replayed.messageCount).toBe(proof.readModelMessageCount);
      await expect(
        bridgePage.locator('[aria-label="Conversation list"] [role="button"]'),
      ).toHaveCount(proof.readModelChatCount);
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
    });

    let acknowledgedProof;
    let acknowledgedBrain;
    await test.step('terminate a second worker and prove acknowledged events never replay', async () => {
      acknowledgedProof = await readSqliteProof(databasePaths);
      acknowledgedBrain = await readBrainSummary(context);
      const oldWorker = worker;
      const oldWorkerInstanceId = (await extensionState(oldWorker)).workerInstanceId;
      const watcher = watchExtensionWorkers(context);
      try {
        const terminated = await terminateExtensionWorker(context, oldWorker);
        expect(terminated.stoppedNormally).toBe(true);
        expect(terminated.stopMethod).toBe('stopWorker');
        await startExtensionWorker(context, terminated.extensionOrigin);
        const restart = await restartedExtensionWorker(context, {
          previousTargetId: terminated.targetId,
          timeoutMs: WORKER_RECOVERY_TIMEOUT_MS,
        });
        worker = restart.worker;
      } finally {
        watcher.stop();
      }

      const replacement = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.socketOpen
          && candidate.sessionBound
          && candidate.outbox?.lastSourceSeq === 8
          && candidate.outbox?.acknowledgedSourceSeq === 8
          && candidate.outbox?.pendingEntries === 0
        ),
        'The second replacement worker did not resume cleanly at acknowledgment 8.',
      );
      expect(replacement.workerInstanceId).not.toBe(oldWorkerInstanceId);
      const rebound = await waitForBrain(
        context,
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== acknowledgedBrain.connectionToken
        ),
        'Brain did not bind a distinct second replacement connection.',
      );
      await sleep(2_000);
      expectStablePersistenceProof(await readSqliteProof(databasePaths), acknowledgedProof);
      expect((await readBrainSummary(context)).viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(rebound.viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
    });

    await test.step('hard-expire a third worker and recover only from the production alarm', async () => {
      // Persistent setup/settings pages poll the worker. Close UI observers so
      // this step measures only the production alarm's ability to wake it.
      for (const page of context.pages()) {
        if (page.url().startsWith('chrome-extension://')) await page.close();
      }
      const before = await readBrainSummary(context);
      const oldWorker = worker;
      const alarm = await waitForSafeAlarmWindow(oldWorker);
      const oldWorkerInstanceId = (await extensionState(oldWorker)).workerInstanceId;
      const watcher = watchExtensionWorkers(context);
      try {
        const terminated = await terminateExtensionWorker(context, oldWorker);
        expect(terminated.stoppedNormally).toBe(true);
        expect(terminated.stopMethod).toBe('stopWorker');

        const retired = await waitForBrain(
          context,
          (candidate) => (
            candidate.agentStatus === 'disconnected'
            && candidate.connectionToken === null
            && candidate.lastHeartbeatAt === null
          ),
          'Brain never hard-retired the terminated worker lease.',
          { timeoutMs: 20_000 },
        );
        expect(retired.connectionToken).toBeNull();
        expect(watcher.creations).toHaveLength(0);
        expect(await extensionWorkerTargetCount(context)).toBe(0);

        const restart = await restartedExtensionWorker(context, {
          previousTargetId: terminated.targetId,
          timeoutMs: WORKER_RECOVERY_TIMEOUT_MS,
        });
        worker = restart.worker;
        expectAlarmCreatedTarget(restart, alarm.scheduledTime);
      } finally {
        watcher.stop();
      }

      const recovery = await worker.evaluate(async () => {
        const saved = await chrome.storage.local.get(['companion_recovery_v1']);
        return saved.companion_recovery_v1 ?? null;
      });
      const cooldownRemaining = Math.max(0, (recovery?.next_attempt_at ?? 0) - Date.now());
      expect(cooldownRemaining).toBeLessThanOrEqual(300_000);
      const alarmReplacement = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.socketOpen
          && candidate.sessionBound
          && candidate.heartbeatTimerPresent
          && candidate.outbox?.lastSourceSeq === 8
          && candidate.outbox?.acknowledgedSourceSeq === 8
          && candidate.outbox?.pendingEntries === 0
        ),
        'The production alarm did not restore a bound, fully acknowledged Agent.',
        { timeoutMs: Math.max(20_000, cooldownRemaining + 60_000) },
      );
      expect(alarmReplacement.workerInstanceId).not.toBe(oldWorkerInstanceId);
      const recovered = await waitForBrain(
        context,
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== null
          && candidate.connectionToken !== before.connectionToken
          && candidate.lastHeartbeatAt !== null
        ),
        'Brain did not observe alarm-driven Agent recovery.',
      );
      expect(recovered.connectionToken).not.toBe(before.connectionToken);
      expectStablePersistenceProof(await readSqliteProof(databasePaths), acknowledgedProof);
      expect(recovered.viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
      await expect(bridgePage.getByRole('heading', { name: 'Inbox' }))
        .toBeVisible({ timeout: 30_000 });
      try {
        await expect(bridgePage.getByRole('article')).toHaveCount(1, { timeout: 30_000 });
      } catch (error) {
        const ui = {
          loading: await bridgePage.getByText('Loading messages…', { exact: true }).count(),
          unavailable: await bridgePage.getByText("Messages couldn't load. Try again.", { exact: true }).count(),
          noStored: await bridgePage.getByText('No messages saved yet', { exact: true }).count(),
          tryAgain: await bridgePage.getByRole('button', { name: 'Try again' }).count(),
        };
        throw new Error(
          `${error.message}\nFinal Inbox state: ${JSON.stringify(ui)}`
          + `\nMessage-page traffic: ${JSON.stringify(bridgeMessageTraffic)}`,
        );
      }
    });

    await test.step('enforce the synthetic safety and no-reload boundary', async () => {
      expect(platform.httpReads.filter((pathValue) => pathValue === '/')).toHaveLength(1);
      expect(platform.httpReads.filter((pathValue) => pathValue === IDENTITY_PATH)).toHaveLength(6);
      expect(platform.httpReads.filter((pathValue) => pathValue === CHATS_PATH)).toHaveLength(1);
      expect(platform.httpReads.filter((pathValue) => pathValue === MESSAGES_PATH)).toHaveLength(1);
      expect(pageErrors).toEqual([]);
      const finalState = await extensionState(worker);
      expectNoDrops(finalState);
      expect(finalState.outbox.lastSourceSeq).toBe(8);
      expect(finalState.outbox.acknowledgedSourceSeq).toBe(8);
      expect(finalState.outbox.pendingEntries).toBe(0);
      platform.assertFailClosed();
    });

    await test.step('replay a fresh page backlog larger than the protected channel queue', async () => {
      await platformPage.reload({ waitUntil: 'domcontentloaded' });
      await expect.poll(() => platformPage.evaluate(
        () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode,
      )).toBe('full');
      await readPlatform(platformPage, IDENTITY_PATH);
      await waitForObservedCreatorIdentity(
        worker, await platformPage.evaluate(() => globalThis.fixtureDocumentToken), SYNTHETIC.creatorId,
      );
      await platformPage.evaluate(() => globalThis.fixtureOpenSocket());
      await brain.stop();
      await waitForExtensionState(worker, (state) => !state.socketOpen && !state.sessionBound,
        'Agent did not observe the desktop stopping before the burst.');
      platform.sendReplayBurst();
      await waitForExtensionState(worker, (state) => state.outbox?.pendingEntries === 20
        && state.outbox?.lastSourceSeq === 28 && state.outbox?.acknowledgedSourceSeq === 8,
      'The twenty-record backlog was not retained while the desktop was offline.');
      await brain.start();
      await waitForExtensionState(worker, (state) => state.socketOpen && state.sessionBound
        && state.outbox?.pendingEntries === 0 && state.outbox?.acknowledgedSourceSeq === 28,
      'The protected connection did not drain the backlog.', { timeoutMs: 60_000 });
      const proof = await readSqliteProof(databasePaths);
      expect(proof.eventSequences).toEqual(Array.from({ length: 28 }, (_, index) => index + 1));
      expect(new Set(proof.eventIds).size).toBe(28);
      expect(proof.canonicalMessageCount).toBe(25);
      expect(platform.httpReads.filter((pathValue) => pathValue === '/')).toHaveLength(1);
      expect(platform.httpReads.filter((pathValue) => pathValue === '/my/chats')).toHaveLength(1);
      expect(platform.httpReads.filter((pathValue) => pathValue === IDENTITY_PATH)).toHaveLength(7);
      expect(platform.httpReads.filter((pathValue) => pathValue === CHATS_PATH)).toHaveLength(1);
      expect(platform.httpReads.filter((pathValue) => pathValue === MESSAGES_PATH)).toHaveLength(1);
      expect(pageErrors).toEqual([]);
      const finalState = await extensionState(worker);
      expectNoDrops(finalState);
      expect(finalState.outbox.lastSourceSeq).toBe(28);
      expect(finalState.outbox.acknowledgedSourceSeq).toBe(28);
      expect(finalState.outbox.pendingEntries).toBe(0);
      platform.assertFailClosed();
    });
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
