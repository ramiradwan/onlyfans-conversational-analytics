import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import { BrainProcess } from '../lib/brain.mjs';
import { readBrainSummary } from '../lib/brain-probe.mjs';
import {
  contentBridgeIsActive,
  extensionState,
  extensionWorker,
  extensionWorkerTargetCount,
  launchExtensionBrowser,
  restartedExtensionWorker,
  startExtensionWorker,
  terminateExtensionWorker,
} from '../lib/extension-browser.mjs';
import { assertBuiltSpa } from '../lib/paths.mjs';
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

function expectStableCanonicalProof(after, before) {
  expect(after.eventIds).toEqual(before.eventIds);
  expect(after.eventSequences).toEqual(before.eventSequences);
  expect(after.eventChangeTypes).toEqual(before.eventChangeTypes);
  expect(after.eventCount).toBe(before.eventCount);
  expect(after.committedSourceSeq).toBe(before.committedSourceSeq);
  expect(after.canonicalChatCount).toBe(before.canonicalChatCount);
  expect(after.canonicalMessageCount).toBe(before.canonicalMessageCount);
  expect(after.readModelChatCount).toBe(before.readModelChatCount);
  expect(after.readModelMessageCount).toBe(before.readModelMessageCount);
  expect(after.viewRevision).toBe(before.viewRevision);
}

test('real MV3 capture proves exact ordering, durable replay, and alarm recovery', async () => {
  test.slow();
  assertBuiltSpa();
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-e2e-capture-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  const databasePath = path.join(temporaryRoot, 'canonical.sqlite3');
  const brain = new BrainProcess({ databasePath });
  let context = null;

  try {
    await test.step('start the real SQLite Brain and unpacked extension', async () => {
      await brain.start();
      context = await launchExtensionBrowser(browserProfile);
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

    let worker = await extensionWorker(context);
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
    const bridgePage = await test.step('prove exact Brain, SQLite, heartbeat, and Inbox state', async () => {
      const first = await waitForBrain(
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.appliedConfigRevision === candidate.requiredConfigRevision
          && candidate.conversationCount === 2
          && candidate.messageCount === 4
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

      const proof = await readSqliteProof(databasePath);
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

      const page = await context.newPage();
      await page.goto('http://localhost:8000/', { waitUntil: 'domcontentloaded' });
      await page.getByRole('link', { name: 'Inbox', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible();
      await expect(page.getByText('Live', { exact: true })).toBeVisible();
      const conversationRows = page.locator('[aria-label="Conversation list"] [role="button"]');
      await expect(conversationRows).toHaveCount(2);

      await page.getByRole('button', {
        name: new RegExp(`^Conversation with ${SYNTHETIC.displayName},`),
      }).click();
      await expect(page.getByRole('article')).toHaveCount(3);
      const primaryMessageRows = await page.getByRole('article').count();

      await page.getByRole('button', {
        name: new RegExp(`^Conversation with Fan ${SYNTHETIC.messageOnlyPeerId},`),
      }).click();
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
          && candidate.outbox?.pendingSequences.join(',') === '7,8'
        ),
        'The offline message-only peer did not remain as pending parent/message sequences 7-8.',
      );
      pendingEventIds = [...pending.outbox.pendingEventIds];
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

      const proof = await readSqliteProof(databasePath);
      expect(proof.committedSourceSeq).toBe(8);
      expect(proof.eventCount).toBe(8);
      expect(proof.eventSequences).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
      expect(proof.eventIds.slice(6)).toEqual(pendingEventIds);
      expect(proof.eventChangeTypes.slice(6)).toEqual(['chat.upsert', 'message.upsert']);
      expect(proof.canonicalChatCount).toBe(3);
      expect(proof.canonicalMessageCount).toBe(5);

      await waitForBrain(
        (candidate) => (
          candidate.agentStatus === 'connected'
          && candidate.connectionToken !== initialConnection
          && candidate.conversationCount === 3
          && candidate.messageCount === 5
        ),
        'Brain did not bind the replacement worker and expose the replayed offline peer.',
      );
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
    });

    let acknowledgedProof;
    let acknowledgedBrain;
    await test.step('terminate a second worker and prove acknowledged events never replay', async () => {
      acknowledgedProof = await readSqliteProof(databasePath);
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
      expectStableCanonicalProof(await readSqliteProof(databasePath), acknowledgedProof);
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
      expectStableCanonicalProof(await readSqliteProof(databasePath), acknowledgedProof);
      expect(recovered.viewRevision).toBe(acknowledgedBrain.viewRevision);
      expect(await platformPage.evaluate(() => globalThis.fixtureDocumentToken))
        .toBe(platformDocumentToken);
      await expect(bridgePage.getByText('Live', { exact: true })).toBeVisible({ timeout: 30_000 });
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
    await brain.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
