import { expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
} from '../../../extension/transport/provisioning-identity.mjs';
import { BRAIN_ORIGIN } from './brain.mjs';
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

async function bridgeRequest(page, pathname, { body = null, method = 'GET' } = {}) {
  return page.evaluate(async ({ pathValue, bodyValue, methodValue }) => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? null;
    if (bodyValue !== null && !csrf) throw new Error('Bridge CSRF token is unavailable.');
    const response = await fetch(pathValue, {
      method: methodValue,
      credentials: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        ...(bodyValue === null ? {} : {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        }),
      },
      ...(bodyValue === null ? {} : { body: JSON.stringify(bodyValue) }),
    });
    return { status: response.status, body: await response.text() };
  }, { pathValue: pathname, bodyValue: body, methodValue: method });
}

function parsedBridgeResponse(response, operation) {
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`${operation} failed (${response.status}).`);
  }
  try { return JSON.parse(response.body); } catch {
    throw new Error(`${operation} returned malformed JSON.`);
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

async function openCompanionPairing(page, accountId) {
  const response = parsedBridgeResponse(await bridgeRequest(
    page,
    '/api/v1/companion/pairings',
    { body: { creator_account_id: accountId }, method: 'POST' },
  ), 'Companion pairing open');
  if (
    typeof response?.pairing_id !== 'string'
    || response.creator_account_id !== accountId
    || !Number.isSafeInteger(response.version)
  ) throw new Error('Brain returned an invalid companion pairing window.');
  return response;
}

async function pairingStatus(page, pairingId) {
  return parsedBridgeResponse(
    await bridgeRequest(page, `/api/v1/companion/pairings/${encodeURIComponent(pairingId)}`),
    'Companion pairing status',
  );
}

async function confirmPairing(page, pairingId, version) {
  return parsedBridgeResponse(await bridgeRequest(
    page,
    `/api/v1/companion/pairings/${encodeURIComponent(pairingId)}/confirm`,
    { body: { version }, method: 'POST' },
  ), 'Companion pairing confirmation');
}

async function waitForPairingState(page, pairingId, expected, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await pairingStatus(page, pairingId);
    if (latest.state === expected) return latest;
    await delay(100);
  }
  throw new Error(`Companion pairing did not reach ${expected}; latest=${latest?.state ?? 'unknown'}.`);
}

async function waitForCode(page, expected, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let latest = '';
  while (Date.now() < deadline) {
    latest = ((await page.locator('#pairing-code').textContent()) ?? '').replace(/\s+/gu, '');
    if (latest === expected) return;
    await delay(100);
  }
  throw new Error(`Extension pairing code ${latest || '<empty>'} did not match Brain code ${expected}.`);
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

async function reloadOnlyFansForFull(popupPage, identityPage, worker, accountId) {
  const reload = popupPage.locator('#reload-tabs');
  await reload.waitFor({ state: 'visible', timeout: 10_000 });
  const reloaded = identityPage.waitForEvent('domcontentloaded', { timeout: 10_000 });
  await reload.click();
  await reloaded;
  await identityPage.evaluate(() => globalThis.__OFCA_E2E_PAIRING_IDENTITY__);
  await waitForDetectedAccount(worker, accountId);
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    const mode = await identityPage.evaluate(
      () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
    );
    if (mode === 'full') break;
    await delay(100);
  }
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
 * Bridge-authorized window creation, /ws/agent/pairing, comparison-code
 * confirmation, Noise session authorization, storage unseal, and the required
 * identity-to-full tab reload. The final external-bind response is a no-op only
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

  const identity = await establishPairingIdentity(context, worker, config.CREATOR_ID);
  const opened = await openCompanionPairing(bridge, config.CREATOR_ID);
  const pairingPage = await context.newPage();
  let routeRemoved = false;
  try {
    await pairingPage.goto(`chrome-extension://${config.EXTENSION_ID}/setup.html`, {
      waitUntil: 'domcontentloaded',
    });
    await expect(pairingPage.locator('#pair-companion')).toBeVisible();
    await pairingPage.locator('#pair-companion').click();
    const awaiting = await waitForPairingState(
      bridge,
      opened.pairing_id,
      'awaiting_confirmation',
    );
    if (typeof awaiting.comparison_code !== 'string' || !/^\d{6}$/u.test(awaiting.comparison_code)) {
      throw new Error('Brain did not expose a valid pairing comparison code.');
    }
    await waitForCode(pairingPage, awaiting.comparison_code);
    const admitted = await confirmPairing(bridge, opened.pairing_id, awaiting.version);
    if (admitted.state !== 'admitted') {
      throw new Error(`Brain did not admit the companion pairing (${admitted.state ?? 'unknown'}).`);
    }
    await waitForBoundFullSession(worker);
    // Setup stays open after confirmation and owns the access reload.
    await reloadOnlyFansForFull(pairingPage, identity.page, worker, config.CREATOR_ID);
    await identity.removeRoute();
    routeRemoved = true;
    await installLegacyBindNoop(worker, config.CREATOR_ID);
  } catch (error) {
    if (!routeRemoved) await identity.removeRoute().catch(() => undefined);
    await identity.page.close().catch(() => undefined);
    throw error;
  } finally {
    await pairingPage.close().catch(() => undefined);
  }

  return {
    creatorAccountId: config.CREATOR_ID,
    extensionId: config.EXTENSION_ID,
    pairingTicket: COMPATIBILITY_MARKER,
    storageBootstrap: COMPATIBILITY_MARKER,
    expiresAt: opened.expires_at,
  };
}
