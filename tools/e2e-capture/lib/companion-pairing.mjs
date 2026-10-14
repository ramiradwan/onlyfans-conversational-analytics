import { expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
} from '../../../extension/transport/provisioning-identity.mjs';
import { BRAIN_ORIGIN } from './brain.mjs';
import { openCompanionPairing, readCompanionPairingStatus, confirmCompanionPairing } from './brain-probe-base.mjs';
import { openPopup, openSetup } from './consent-ui.mjs';
import { observeProductTabReloads } from './extension-browser.mjs';
import { EXTENSION_DIST, PRODUCT_ROOT } from './paths.mjs';

const PACKAGED_GRANT_TRUST = path.join(EXTENSION_DIST, 'companion-grant-trust.json');
const QUALIFICATION_GRANT_TRUST = path.join(
  PRODUCT_ROOT,
  'contracts',
  'companion-pairing-v1',
  'trust-set.json',
);
const PAIRING_IDENTITY_ROUTE = 'https://onlyfans.com/**';

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function activeWorker(context) {
  const worker = context.serviceWorkers().find((candidate) => (
    candidate.url().startsWith('chrome-extension://')
    && candidate.url().endsWith('/background.js')
  ));
  if (!worker) throw new Error('The active MV3 worker is unavailable for pairing qualification.');
  return worker;
}

function bridgePage(context) {
  const page = context.pages().find((candidate) => {
    try { return new URL(candidate.url()).origin === BRAIN_ORIGIN; } catch { return false; }
  });
  if (!page) throw new Error('The authenticated Bridge page is unavailable for pairing qualification.');
  return page;
}

function installQualificationTrust(context) {
  const original = readFileSync(PACKAGED_GRANT_TRUST);
  const trust = JSON.parse(readFileSync(QUALIFICATION_GRANT_TRUST, 'utf8'));
  if (
    trust.production_usable !== false
    || !Array.isArray(trust.keys)
    || trust.keys.length === 0
    || !trust.keys.every((entry) => entry?.fixture_only === true)
  ) {
    throw new Error('The pinned E2E pairing trust fixture is not explicitly test-only.');
  }
  writeFileSync(
    PACKAGED_GRANT_TRUST,
    `${JSON.stringify({ ...trust, production_usable: true }, null, 2)}\n`,
    'utf8',
  );
  let restored = false;
  const restore = () => {
    if (restored) return;
    restored = true;
    writeFileSync(PACKAGED_GRANT_TRUST, original);
  };
  context.once('close', restore);
  return restore;
}

async function assertQualificationTrustVisible(worker) {
  const usable = await worker.evaluate(async () => {
    const response = await fetch(chrome.runtime.getURL('companion-grant-trust.json'), {
      cache: 'no-store',
    });
    const trust = await response.json();
    return trust.production_usable === true
      && Array.isArray(trust.keys)
      && trust.keys.length > 0
      && trust.keys.every((entry) => entry?.fixture_only === true);
  });
  if (!usable) throw new Error('The E2E pairing trust resource was not visible to the extension.');
}

async function waitForDetectedAccount(worker, accountId, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const detected = await worker.evaluate(async ({ key, schema, account }) => {
      const stored = await chrome.storage.session.get([key]);
      const document = stored[key];
      return document?.schema === schema
        && Array.isArray(document.contexts)
        && document.contexts.some((context) => (
          context?.observed_platform_id === account
          && typeof context?.consent_epoch === 'string'
        ));
    }, {
      key: PROVISIONING_IDENTITY_STORAGE_KEY,
      schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
      account: accountId,
    });
    if (detected) return;
    await delay(100);
  }
  throw new Error(`The shipping identity bridge did not observe ${accountId}.`);
}

async function establishPairingIdentity(context, worker, accountId) {
  const handler = async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() !== 'GET') {
      await route.abort('blockedbyclient');
      return;
    }
    if (url.pathname === '/') {
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        headers: { 'cache-control': 'no-store' },
        body: `<!doctype html><html><body><script>
          globalThis.__OFCA_E2E_DOCUMENT__ = crypto.randomUUID();
          globalThis.__OFCA_E2E_PAIRING_IDENTITY__ = fetch('/api2/v2/users/me', {
            credentials: 'include', cache: 'no-store'
          }).then((response) => response.json());
        </script></body></html>`,
      });
      return;
    }
    if (url.pathname === '/api2/v2/users/me') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json; charset=utf-8',
        headers: { 'cache-control': 'no-store' },
        body: JSON.stringify({ id: accountId }),
      });
      return;
    }
    if (url.pathname === '/favicon.ico') {
      await route.fulfill({ status: 204, body: '' });
      return;
    }
    await route.abort('blockedbyclient');
  };

  await context.route(PAIRING_IDENTITY_ROUTE, handler);
  const page = await context.newPage();
  try {
    await page.goto('https://onlyfans.com/?ofca-e2e-pairing=1', {
      waitUntil: 'domcontentloaded',
    });
    await page.evaluate(() => globalThis.__OFCA_E2E_PAIRING_IDENTITY__);
    await waitForDetectedAccount(worker, accountId);
    return {
      page,
      async removeRoute() {
        await context.unroute(PAIRING_IDENTITY_ROUTE, handler);
      },
    };
  } catch (error) {
    await context.unroute(PAIRING_IDENTITY_ROUTE, handler).catch(() => undefined);
    await page.close().catch(() => undefined);
    throw error;
  }
}

function servedRuntimeConfig(page) {
  return page.locator('#fastapi-config').textContent().then((text) => {
    if (typeof text !== 'string' || text.length === 0) {
      throw new Error('Bridge runtime configuration is unavailable.');
    }
    const config = JSON.parse(text);
    if (
      typeof config?.CREATOR_ID !== 'string'
      || typeof config?.EXTENSION_ID !== 'string'
      || !/^[a-p]{32}$/u.test(config.EXTENSION_ID)
    ) throw new Error('Bridge runtime configuration is incomplete.');
    return config;
  });
}

async function waitForBoundFullSession(worker, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await worker.evaluate(() => globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__());
    if (latest?.capturePhase === 'full' && latest?.sessionBound === true) return latest;
    await delay(100);
  }
  throw new Error(
    `Companion pairing completed without a bound Full session; phase=${latest?.capturePhase ?? 'unknown'} session=${latest?.sessionBound ?? false}.`,
  );
}

async function assertSameDocumentFull(pairingPage, identityPage, worker, accountId, documentId) {
  await expect(pairingPage.locator('#reload-tabs')).toBeHidden();
  await waitForDetectedAccount(worker, accountId);
  await expect.poll(() => identityPage.evaluate(
    () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
  )).toBe('full');
  expect(await identityPage.evaluate(() => globalThis.__OFCA_E2E_DOCUMENT__)).toBe(documentId);
  await waitForBoundFullSession(worker);
}

/** Capture-protocol fixture, not native-host/automatic-task qualification.
 * Keep real content identity, authenticated operator admission, independent
 * comparison, signed grants, Noise authorization and durable pin/storage commit.
 * The browser test supplies the explicit operator actions through the retained
 * comparison-recovery API; installed qualification covers native task dispatch.
 */
export async function pairCompanionForCapture(context) {
  const bridge = bridgePage(context);
  await bridge.reload({ waitUntil: 'domcontentloaded' });
  const config = await servedRuntimeConfig(bridge);
  const worker = activeWorker(context);
  installQualificationTrust(context);
  await assertQualificationTrustVisible(worker);
  const productReloads = await observeProductTabReloads(worker);
  const identity = await establishPairingIdentity(context, worker, config.CREATOR_ID);
  const documentId = await identity.page.evaluate(() => globalThis.__OFCA_E2E_DOCUMENT__);
  const popup = context.pages().find(page => page.url() === `chrome-extension://${config.EXTENSION_ID}/popup.html`)
    ?? await openPopup(context, config.EXTENSION_ID, []);
  const setup = await openSetup(popup);
  let routeRemoved = false;
  try {
    const opened = await openCompanionPairing(context);
    expect(opened.creatorAccountId).toBe(config.CREATOR_ID);
    await setup.evaluate(() => {
      if (globalThis.__capturePairingPort) throw Error('fixture_pairing_already_started');
      const port = chrome.runtime.connect({ name: 'ofca.companion.pairing' });
      globalThis.__capturePairingPort = port;
      globalThis.__capturePairingStatus = null;
      port.onMessage.addListener(value => {
        if (typeof value?.state === 'string') globalThis.__capturePairingStatus = value;
      });
      port.postMessage({ type: 'pair' });
    });
    await expect.poll(() => setup.evaluate(() => globalThis.__capturePairingStatus?.state),
      { timeout: 20_000 }).toBe('compare');
    const code = await setup.evaluate(() => globalThis.__capturePairingStatus.comparison_code);
    expect(code).toMatch(/^[0-9]{6}$/u);
    let current;
    await expect.poll(async () => {
      current = await readCompanionPairingStatus(context, opened.pairing_id);
      return current.state;
    }, { timeout: 20_000 }).toBe('awaiting_confirmation');
    expect(current.pairing_id).toBe(opened.pairing_id);
    expect(current.creator_account_id).toBe(config.CREATOR_ID);
    expect(current.comparison_code).toBe(code);
    const confirmed = await confirmCompanionPairing(context, opened.pairing_id, current.version, code);
    // Confirmation returns the admitted durable pin, not the window that it
    // consumed. The Agent must still finish its independent storage commit.
    expect(confirmed.state).toBe('admitted');
    expect(confirmed.pairing_id).toBe(opened.pairing_id);
    expect(confirmed.creator_account_id).toBe(config.CREATOR_ID);
    await expect.poll(() => setup.evaluate(() => globalThis.__capturePairingStatus?.state),
      { timeout: 20_000 }).toBe('paired');
    await waitForBoundFullSession(worker);
    await assertSameDocumentFull(setup, identity.page, worker, config.CREATOR_ID, documentId);
    expect(await productReloads()).toEqual([]);
    await identity.removeRoute();
    routeRemoved = true;
    return { creatorAccountId: config.CREATOR_ID, extensionId: config.EXTENSION_ID,
      pairingId: opened.pairing_id, expiresAt: opened.expires_at };
  } finally {
    if (!routeRemoved) {
      await identity.removeRoute().catch(() => undefined);
      await identity.page.close().catch(() => undefined);
    }
  }
}
