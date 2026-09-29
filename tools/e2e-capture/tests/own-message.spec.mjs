import { mkdtemp, rm } from 'node:fs/promises';
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

test('an own message pushed by the platform socket reaches the Brain as outbound', async () => {
  test.setTimeout(300_000);
  assertBuiltSpa(); assertBuiltExtension();
  const root = await mkdtemp(path.join(tmpdir(), 'own-message-'));
  let context;
  let brain;
  try {
    context = await launchExtensionBrowser(path.join(root, 'profile'));
    const worker = await extensionWorker(context, { timeoutMs: 15_000 });
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
      { timeout: 12_000 }).toBe('full');
    await page.evaluate(() => globalThis.fixtureRead('/api2/v2/users/me'));
    await expect.poll(async () => {
      const summary = await readBrainSummary(context);
      const state = await extensionState(worker);
      return summary.agentStatus === 'connected' && state.sessionBound
        && state.enabledResources?.includes('messages') === true;
    }, { timeout: 90_000 }).toBe(true);
    await page.evaluate(() => globalThis.fixtureOpenSocket());
    await expect.poll(() => platform.openSockets.size).toBeGreaterThan(0);
    platform.sendOwnMessageEcho();
    await expect.poll(async () => (await readBrainSummary(context)).messageCount,
      { timeout: 30_000 }).toBe(1);
    await expect.poll(async () => binding.evaluate(async (chatId) => {
      const response = await fetch(`/api/v1/conversations/${encodeURIComponent(chatId)}/messages?limit=100`, {
        credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      if (!response.ok) return null;
      const { items } = await response.json();
      return items.find((item) => item.message_id === 'fixture-own-echo')?.direction ?? null;
    }, SYNTHETIC.chatId), { timeout: 30_000 }).toBe('outbound');
    expect(errors).toEqual([]);
    platform.assertFailClosed();
  } finally {
    await context?.close().catch(() => undefined);
    await brain?.stop().catch(() => undefined);
    await rm(root, { recursive: true, force: true });
  }
});
