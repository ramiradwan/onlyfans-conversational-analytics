import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BRAIN_ORIGIN, BrainProcess } from '../lib/brain.mjs';
import {
  readBrainSummary,
  requestAgentPairingTicket,
} from '../lib/brain-probe.mjs';
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
  await expect.poll(async () => {
    latest = await extensionState(worker);
    return predicate(latest);
  }, { message, timeout: timeoutMs }).toBe(true);
  return latest;
}

async function waitForBrain(predicate, message, { timeoutMs = 25_000 } = {}) {
  let latest = null;
  await expect.poll(async () => {
    try {
      latest = await readBrainSummary();
      return predicate(latest);
    } catch {
      return false;
    }
  }, { message, timeout: timeoutMs }).toBe(true);
  return latest;
}

async function waitForSafeAlarmWindow(worker) {
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
  test.slow();
  assertBuiltSpa();
  assertBuiltExtension();
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-e2e-capture-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  const canonicalDatabasePath = path.join(temporaryRoot, 'canonical.sqlite3');
  const projectionDatabasePath = path.join(temporaryRoot, 'projections.sqlite3');
  const databasePaths = { canonicalDatabasePath, projectionDatabasePath };
  let brain = null;
  let context = null;
  let worker = null;

  try {
    await test.step('load the audited MV3 artifact and pair it through the exact Bridge origin', async () => {
      context = await launchExtensionBrowser(browserProfile);
      worker = await extensionWorker(context);
      const actualExtensionId = extensionId(worker);
      brain = new BrainProcess({
        ...databasePaths,
        extensionId: actualExtensionId,
      });
      await brain.start();

      const pairing = await requestAgentPairingTicket();
      expect(pairing.creatorAccountId).toBe('dev-creator-account');
      expect(pairing.extensionId).toBe(actualExtensionId);
      const bindingPage = context.pages()[0] ?? await context.newPage();
      await bindingPage.goto(`${BRAIN_ORIGIN}/health`, { waitUntil: 'domcontentloaded' });
      await bindAgentFromBridgePage(bindingPage, {
        extensionId: actualExtensionId,
        creatorAccountId: pairing.creatorAccountId,
        authTicket: pairing.pairingTicket,
      });
    });

    const platform = new SyntheticPlatform();
    await platform.install(context);
    const platformPage = context.pages()[0] ?? await context.newPage();
    const pageErrors = [];
    platformPage.on('pageerror', (error) => pageErrors.push(error.message));
    await platformPage.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded' });
    const platformDocumentToken = await platformPage.evaluate(
      () => globalThis.fixtureDocumentToken,
    );

    await test.step('prove both page worlds and Brain-owned capture policy are active', async () => {
      await expect.poll(
        () => platformPage.evaluate(() => globalThis.__OFCA_PAGE_HOOK_ACTIVE__ === true),
      ).toBe(true);
      await expect.poll(() => contentBridgeIsActive(worker)).toBe(true);
      const state = await waitForExtensionState(
        worker,
        (candidate) => (
          candidate.runtimeReady
          && candidate.socketOpen
          && candidate.sessionBound
          && candidate.heartbeatTimerPresent
          && candidate.syncRequired === false
          && candidate.appliedConfigRevision === 'config-8'
          && candidate.enabledResources.includes('chats')
          && candidate.enabledResources.includes('messages')
          && candidate.reconcileAlarm?.periodInMinutes === 1
        ),
        'Agent did not bind and apply config-8 chat/message capture policy.',
      );
      expect(state.outbox).not.toBeNull();
      expect(state.outbox.lastSourceSeq).toBe(0);
      expect(state.outbox.acknowledgedSourceSeq).toBe(0);
      expect(state.outbox.pendingEntries).toBe(0);
    });

    await test.step('produce exactly one chat and four message observations as sequence 1-6', async () => {
      await readPlatform(platformPage, IDENTITY_PATH);
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
        dashboard.getByRole('heading', { name: 'Creator dashboard' }),
      ).toBeVisible();
      for (const [title, value] of [
        ['Total conversations', '2+'],
        ['Total messages', '4+'],
        ['Inbound messages', '3+'],
        ['Outbound messages', '1+'],
      ]) {
        const card = dashboard.getByText(title, { exact: true }).locator('..');
        await expect(card.getByText(value, { exact: true })).toBeVisible();
        await expect(
          card.getByText(/Based on synced messages · sample \d+ · As of/),
        ).toBeVisible();
      }
      await expect(dashboard.getByText('Ask is not connected')).toBeVisible();
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
      await expect(page.getByRole('main').getByText('Degraded', { exact: true })).toBeVisible();
      const conversationRows = page.locator('[aria-label="Conversation list"] [role="button"]');
      await expect(conversationRows).toHaveCount(2);

      await expect(page.getByRole('article')).toHaveCount(1);
      await expect(page.getByRole('button', { name: 'No earlier stored messages' })).toBeVisible();

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
      await expect(page.getByRole('button', { name: 'No earlier stored messages' })).toBeVisible();
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

    let pendingEventIds;
    await test.step('persist exact pending sequences 7-8 while Brain is unavailable', async () => {
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
      expect(outboxProof.changeTypes).toEqual(['chat.upsert', 'message.upsert']);
      pendingEventIds = outboxProof.eventIds;
      expect(pendingEventIds).toHaveLength(2);
      expect(new Set(pendingEventIds).size).toBe(2);
      expectNoDrops(pending);
      expect(platform.websocketFramesSent).toBe(2);
    });

    await test.step('terminate the worker and replay the same IDs after a Brain restart', async () => {
      const oldWorker = worker;
      const oldWorkerInstanceId = (await extensionState(oldWorker)).workerInstanceId;
      const watcher = watchExtensionWorkers(context);
      try {
        const terminated = await terminateExtensionWorker(context, oldWorker);
        expect(terminated.stoppedNormally).toBe(true);
        expect(terminated.stopMethod).toBe('stopWorker');
        await brain.start();
        await startExtensionWorker(context, terminated.extensionOrigin);
        const restart = await restartedExtensionWorker(context, {
          previousTargetId: terminated.targetId,
          timeoutMs: WORKER_RECOVERY_TIMEOUT_MS,
        });
        worker = restart.worker;
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
        'Alarm-restored worker did not replay and acknowledge exact sequences 7-8.',
      );
      expect(recovered.heartbeatTimerPresent).toBe(true);
      expect(recovered.workerInstanceId).not.toBe(oldWorkerInstanceId);

      const proof = await readSqliteProof(databasePaths);
      expect(proof.committedSourceSeq).toBe(8);
      expect(proof.eventCount).toBe(8);
      expect(proof.eventSequences).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
      expect(proof.eventIds.slice(6)).toEqual(pendingEventIds);
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

      const replayed = await waitForBrain(
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== initialConnection
          && candidate.conversationCount === 3
          && candidate.messageCount === 5
        ),
        'Brain did not bind the replacement worker and expose the replayed offline peer.',
      );
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
      acknowledgedBrain = await readBrainSummary();
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
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== acknowledgedBrain.connectionToken
        ),
        'Brain did not bind a distinct second replacement connection.',
      );
      await sleep(2_000);
      expectStablePersistenceProof(await readSqliteProof(databasePaths), acknowledgedProof);
      expect((await readBrainSummary()).viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(rebound.viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
    });

    await test.step('hard-expire a third worker and recover only from the production alarm', async () => {
      const before = await readBrainSummary();
      const oldWorker = worker;
      const alarm = await waitForSafeAlarmWindow(oldWorker);
      const oldWorkerInstanceId = (await extensionState(oldWorker)).workerInstanceId;
      const watcher = watchExtensionWorkers(context);
      try {
        const terminated = await terminateExtensionWorker(context, oldWorker);
        expect(terminated.stoppedNormally).toBe(true);
        expect(terminated.stopMethod).toBe('stopWorker');

        const retired = await waitForBrain(
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
      );
      expect(alarmReplacement.workerInstanceId).not.toBe(oldWorkerInstanceId);
      const recovered = await waitForBrain(
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
          unavailable: await bridgePage.getByText('Message history is unavailable.', { exact: true }).count(),
          noStored: await bridgePage.getByText('No stored messages yet', { exact: true }).count(),
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
      expect(platform.httpReads.filter((pathValue) => pathValue === IDENTITY_PATH)).toHaveLength(1);
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
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
