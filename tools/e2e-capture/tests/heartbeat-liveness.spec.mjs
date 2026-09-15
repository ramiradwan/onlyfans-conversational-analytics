import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { BRAIN_ORIGIN, BrainProcess } from '../lib/brain.mjs';
import {
  establishBrowserWebAuthnSession,
  readBrainSummary,
  requestAgentPairingTicket,
} from '../lib/brain-probe.mjs';
import { connectFullAnalytics, openPopup } from '../lib/consent-ui.mjs';
import {
  bindAgentFromBridgePage,
  extensionId,
  extensionState,
  extensionWorker,
  extensionWorkerTargetCount,
  launchExtensionBrowser,
} from '../lib/extension-browser.mjs';
import { assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForConnectedBrain(context) {
  let latest = null;
  await expect.poll(async () => {
    latest = await readBrainSummary(context);
    return latest.agentStatus === 'connected'
      && latest.connectionToken !== null
      && latest.appliedConfigRevision === latest.requiredConfigRevision;
  }, {
    message: 'Agent did not reach a connected/configured Brain lease.',
    timeout: 25_000,
  }).toBe(true);
  return latest;
}

test('paired Agent heartbeat advances without capture traffic', async () => {
  test.slow();
  assertBuiltSpa();
  assertBuiltExtension();

  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-e2e-heartbeat-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  const canonicalDatabasePath = path.join(temporaryRoot, 'canonical.sqlite3');
  const authDatabasePath = path.join(temporaryRoot, 'auth.sqlite3');
  const projectionDatabasePath = path.join(temporaryRoot, 'projections.sqlite3');

  let brain = null;
  let context = null;
  let worker = null;
  try {
    context = await launchExtensionBrowser(browserProfile);
    worker = await extensionWorker(context);
    const actualExtensionId = extensionId(worker);
    brain = new BrainProcess({
      authDatabasePath,
      canonicalDatabasePath,
      projectionDatabasePath,
      extensionId: actualExtensionId,
    });
    await brain.start();

    const pageErrors = [];
    const popup = await openPopup(context, actualExtensionId, pageErrors);
    await connectFullAnalytics(context, popup, worker);
    await expect.poll(async () => (await extensionState(worker)).capturePhase, {
      message: 'Granting both origins did not move capture into the identity phase.',
    }).toBe('identity');
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
    await expect.poll(async () => (await extensionState(worker)).capturePhase, {
      message: 'Pairing did not reconcile the capture phase to full.',
    }).toBe('full');

    // The external E2E binding helper is intentionally not itself an internal
    // Agent wake event. Reopen the real popup once: its normal status requests
    // exercise chrome.runtime.onMessage and therefore the production wake path,
    // without creating capture traffic or a test-only transport hook.
    const wakePopup = await openPopup(context, actualExtensionId, pageErrors);
    await wakePopup.close();

    const first = await waitForConnectedBrain(context);
    const before = await extensionState(worker);
    await sleep(2_200);

    try {
      let second = null;
      await expect.poll(async () => {
        second = await readBrainSummary(context);
        return second.agentStatus === 'connected'
          && second.connectionToken === first.connectionToken
          && Date.parse(second.lastHeartbeatAt) > Date.parse(first.lastHeartbeatAt);
      }, {
        message: 'Agent heartbeat did not advance on the original connection.',
        timeout: 10_000,
      }).toBe(true);
    } catch (error) {
      let after = null;
      let stateError = null;
      try {
        after = await extensionState(worker);
      } catch (diagnosticError) {
        stateError = diagnosticError instanceof Error ? diagnosticError.message : String(diagnosticError);
      }
      const workerTargets = await extensionWorkerTargetCount(context).catch(() => -1);
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}\n`
        + `Bridge origin: ${BRAIN_ORIGIN}\n`
        + `Initial Brain: ${JSON.stringify(first)}\n`
        + `Extension before wait: ${JSON.stringify(before)}\n`
        + (stateError === null
          ? `Extension after failure: ${JSON.stringify(after)}\n`
          : `Extension after failure unavailable: ${stateError}\n`)
        + `Extension worker target count: ${workerTargets}\n`
        + `Brain output: ${brain.recentOutput()}`,
        { cause: error },
      );
    }
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
