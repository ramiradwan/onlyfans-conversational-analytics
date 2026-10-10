import { expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
} from '../../../extension/transport/provisioning-identity.mjs';
import { BRAIN_ORIGIN } from './brain.mjs';
import { openPopup, openSetup } from './consent-ui.mjs';
import { observeProductTabReloads } from './extension-browser.mjs';
import { EXTENSION_DIST, PRODUCT_ROOT } from './paths.mjs';

const COMPATIBILITY_MARKER = 'e2e-companion-pairing-v1';
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

async function installLegacyBindNoop(worker, accountId) {
  await worker.evaluate(({ account, marker, bridgeOrigin }) => {
    if (globalThis.__OFCA_E2E_BIND_COMPATIBILITY__ === true) return;
    chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
      let origin = null;
      try { origin = new URL(sender?.url ?? '').origin; } catch {}
      if (
        origin !== bridgeOrigin
        || message?.type !== 'ofca.agent.bind'
        || message?.protocol_version !== '2'
        || message?.creator_account_id !== account
        || message?.auth_ticket !== marker
        || message?.storage_bootstrap !== marker
      ) return false;
      sendResponse({ ok: true, code: 'already_paired' });
      return true;
    });
    Object.defineProperty(globalThis, '__OFCA_E2E_BIND_COMPATIBILITY__', {
      configurable: false,
      enumerable: false,
      value: true,
      writable: false,
    });
  }, { account: accountId, marker: COMPATIBILITY_MARKER, bridgeOrigin: BRAIN_ORIGIN });
}

/**
 * Compatibility wrapper for the long capture scenario. The historical helper
 * name is retained so the rest of the proof stays unchanged, but this function
 * now performs the shipping companion flow: content-script identity discovery,
 * the App-owned automatic pairing and confirmation, Noise session authorization,
 * storage unseal, and same-document
 * identity-to-full attachment. The final external-bind response is a no-op only
 * because the old call site still invokes a transport that no longer exists;
 * pairing and the bound Full session have already succeeded before it is armed.
 */
export async function requestAgentPairingTicket(context) {
  const bridge = bridgePage(context);
  await bridge.reload({ waitUntil: 'domcontentloaded' });
  const config = await servedRuntimeConfig(bridge);
  const worker = activeWorker(context);
  installQualificationTrust(context);
  await assertQualificationTrustVisible(worker);

  const productReloads = await observeProductTabReloads(worker);
  const identity = await establishPairingIdentity(context, worker, config.CREATOR_ID);
  const documentId = await identity.page.evaluate(() => globalThis.__OFCA_E2E_DOCUMENT__);
  const popup = context.pages().find((page) => page.url() === `chrome-extension://${config.EXTENSION_ID}/popup.html`)
    ?? await openPopup(context, config.EXTENSION_ID, []);
  const pairingPage = await openSetup(popup);
  const journey = new URLSearchParams(new URL(pairingPage.url()).hash.slice(1)).get('journey');
  expect(journey).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u);
  let opened = null;
  let routeRemoved = false;
  try {
    const creation = pairingPage.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/companion/pairings');
    const confirmation = pairingPage.waitForResponse((response) => response.request().method() === 'POST'
      && /^\/api\/v1\/companion\/pairings\/[^/]+\/confirm$/u.test(new URL(response.url()).pathname));
    // A failed creation must remain the reported cause when teardown closes a
    // pending confirmation wait. Awaiting either original still rejects.
    void creation.catch(() => undefined);
    void confirmation.catch(() => undefined);
    // Resume the authenticated App in the same registered workspace, exactly as
    // the completed desktop/hosted continuation does. No Settings click or
    // direct test POST starts or confirms the connection.
    await pairingPage.goto(`${BRAIN_ORIGIN}/#journey=${journey}`, { waitUntil: 'domcontentloaded' });
    const created = await creation;
    expect(created.ok()).toBe(true);
    expect(created.request().postDataJSON().creator_account_id).toBe(config.CREATOR_ID);
    const confirmed = await confirmation;
    expect(confirmed.ok()).toBe(true);
    const pairingPath = new URL(confirmed.url()).pathname.replace(/\/confirm$/u, '');
    // App completion may navigate immediately after the response. Read the
    // committed result through the authenticated owner instead of depending on
    // a response body tied to a document that Chrome has already discarded.
    const admitted = await bridge.evaluate(async (pathname) => {
      const response = await fetch(pathname, { credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const reason = typeof body?.detail === 'string' && /^pairing_[a-z_]{1,48}$/u.test(body.detail)
          ? body.detail : 'unavailable';
        // Read-only evidence: never replay confirmation or expose pairing keys.
        let pinState = 'unavailable';
        try {
          const pinsResponse = await fetch('/api/v1/companion/pins', {
            credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          });
          if (!pinsResponse.ok) pinState = `HTTP ${pinsResponse.status}`;
          else {
            const result = await pinsResponse.json();
            pinState = Array.isArray(result?.pins)
              ? result.pins.some((pin) => pathname.endsWith(`/${pin.pairing_id}`)) ? 'admitted' : 'absent'
              : 'invalid';
          }
        } catch { /* The pin list cannot be confirmed. */ }
        throw new Error(`The confirmed pairing could not be read (HTTP ${response.status}; reason=${reason}; pin=${pinState}).`);
      }
      return response.json();
    }, pairingPath);
    expect(admitted.state).toBe('admitted');
    expect(admitted.creator_account_id).toBe(config.CREATOR_ID);
    expect(pairingPath).toBe(`/api/v1/companion/pairings/${admitted.pairing_id}`);
    opened = admitted;
    await waitForBoundFullSession(worker);
    await assertSameDocumentFull(pairingPage, identity.page, worker, config.CREATOR_ID, documentId);
    expect(await productReloads()).toEqual([]);
    await identity.removeRoute();
    routeRemoved = true;
    await installLegacyBindNoop(worker, config.CREATOR_ID);
  } catch (error) {
    if (!routeRemoved) await identity.removeRoute().catch(() => undefined);
    await identity.page.close().catch(() => undefined);
    throw error;
  }

  return {
    creatorAccountId: config.CREATOR_ID,
    extensionId: config.EXTENSION_ID,
    pairingTicket: COMPATIBILITY_MARKER,
    storageBootstrap: COMPATIBILITY_MARKER,
    expiresAt: opened.expires_at,
  };
}
