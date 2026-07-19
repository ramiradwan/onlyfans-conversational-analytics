import { chromium } from '@playwright/test';

import { EXTENSION_DIST } from './paths.mjs';

const RECONCILE_ALARM_NAME = 'ofca-agent-reconcile';

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function launchExtensionBrowser(userDataDir) {
  return chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1280, height: 800 },
    serviceWorkers: 'allow',
    args: [
      `--disable-extensions-except=${EXTENSION_DIST}`,
      `--load-extension=${EXTENSION_DIST}`,
      '--host-resolver-rules=MAP bridge.localhost 127.0.0.1',
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-default-apps',
      '--disable-sync',
      '--metrics-recording-only',
      '--no-default-browser-check',
      '--no-first-run',
    ],
  });
}

export function extensionId(worker) {
  const id = new URL(worker.url()).hostname;
  if (!/^[a-p]{32}$/.test(id)) {
    throw new Error(`Chromium returned an invalid unpacked extension ID: ${id}`);
  }
  return id;
}

export async function bindAgentFromBridgePage(
  page,
  { extensionId: targetExtensionId, creatorAccountId, authTicket },
) {
  return page.evaluate(
    ({ extensionId: id, creatorAccountId: account, authTicket: ticket }) => (
      new Promise((resolve, reject) => {
        if (typeof globalThis.chrome?.runtime?.sendMessage !== 'function') {
          reject(new Error('Chrome external messaging is unavailable at the Bridge origin.'));
          return;
        }
        chrome.runtime.sendMessage(id, {
          type: 'ofca.agent.bind',
          protocol_version: '2',
          creator_account_id: account,
          auth_ticket: ticket,
        }, (response) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
            return;
          }
          if (response?.ok !== true) {
            reject(new Error(`Agent rejected pairing: ${response?.code ?? 'unknown'}`));
            return;
          }
          resolve(response);
        });
      })
    ),
    {
      extensionId: targetExtensionId,
      creatorAccountId,
      authTicket,
    },
  );
}

export async function extensionOutboxProof(worker) {
  return worker.evaluate(async () => {
    const databases = await indexedDB.databases();
    const matches = databases.filter(({ name }) => (
      typeof name === 'string' && name.startsWith('onlyfans-agent-account-v2-')
    ));
    if (matches.length !== 1) {
      throw new Error(`Expected one account-partitioned Agent database, found ${matches.length}.`);
    }
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open(matches[0].name);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error('Unable to open Agent IndexedDB.'));
    });
    try {
      const rows = await new Promise((resolve, reject) => {
        const request = database.transaction(['outbox'], 'readonly')
          .objectStore('outbox')
          .getAll();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error ?? new Error('Unable to read Agent outbox.'));
      });
      return {
        eventIds: rows.map((row) => row.event_id),
        sequences: rows.map((row) => row.source_seq),
        changeTypes: rows.map((row) => row.change?.type ?? null),
      };
    } finally {
      database.close();
    }
  });
}

export async function extensionWorker(context, { differentFrom = null, timeoutMs = 15_000 } = {}) {
  const acceptable = (worker) => (
    worker !== differentFrom
    && worker.url().startsWith('chrome-extension://')
    && worker.url().endsWith('/background.js')
  );
  const existing = context.serviceWorkers().find(acceptable);
  if (existing) return existing;

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remaining = Math.max(1, deadline - Date.now());
    const worker = await Promise.race([
      context.waitForEvent('serviceworker', { timeout: remaining }).catch(() => null),
      delay(Math.min(remaining, 100)).then(() => null),
    ]);
    if (worker && acceptable(worker)) return worker;
    const candidate = context.serviceWorkers().find(acceptable);
    if (candidate) return candidate;
  }
  throw new Error('The unpacked MV3 extension did not start a service worker.');
}

function isExtensionTarget(candidate) {
  return candidate.type === 'service_worker'
    && candidate.url.startsWith('chrome-extension://')
    && candidate.url.endsWith('/background.js');
}

function extensionOrigin(workerUrl) {
  const parsed = new URL(workerUrl);
  return `${parsed.protocol}//${parsed.host}`;
}

export async function extensionWorkerTargetCount(context) {
  const browser = context.browser();
  if (!browser) throw new Error('Chromium browser connection is unavailable.');
  const cdp = await browser.newBrowserCDPSession();
  try {
    const { targetInfos } = await cdp.send('Target.getTargets');
    return targetInfos.filter(isExtensionTarget).length;
  } finally {
    await cdp.detach();
  }
}

export async function restartedExtensionWorker(
  context,
  { previousTargetId, timeoutMs = 90_000 },
) {
  const browser = context.browser();
  if (!browser) throw new Error('Chromium browser connection is unavailable.');
  const cdp = await browser.newBrowserCDPSession();
  const deadline = Date.now() + timeoutMs;
  try {
    while (Date.now() < deadline) {
      const { targetInfos } = await cdp.send('Target.getTargets');
      const target = targetInfos.find(isExtensionTarget);
      if (target) {
        const createdAt = Date.now();
        while (Date.now() < deadline) {
          for (const worker of context.serviceWorkers()) {
            if (!worker.url().startsWith('chrome-extension://')) continue;
            if (!worker.url().endsWith('/background.js')) continue;
            try {
              const ready = await worker.evaluate(
                () => typeof globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__ === 'function',
              );
              if (ready) {
                return {
                  worker,
                  targetId: target.targetId,
                  reusedTargetId: target.targetId === previousTargetId,
                  createdAt,
                };
              }
            } catch {
              // Playwright may retain the just-closed worker object until the new target attaches.
            }
          }
          await delay(50);
        }
      }
      await delay(100);
    }
  } finally {
    await cdp.detach();
  }
  throw new Error('The reconciliation alarm did not create a new MV3 worker target.');
}

export async function extensionState(worker) {
  return worker.evaluate(
    (alarmName) => globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__(alarmName),
    RECONCILE_ALARM_NAME,
  );
}

export async function contentBridgeIsActive(worker) {
  return worker.evaluate(async () => {
    const [tab] = await chrome.tabs.query({ url: ['https://onlyfans.com/*'] });
    if (!tab || !Number.isInteger(tab.id)) return false;
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: 'ISOLATED',
      func: () => globalThis.__OFCA_CAPTURE_BRIDGE_ACTIVE__ === true,
    });
    return result?.[0]?.result === true;
  });
}

async function waitForTargetClosure(cdp, targetId, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const { targetInfos } = await cdp.send('Target.getTargets');
    if (!targetInfos.some((candidate) => candidate.targetId === targetId)) return;
    await delay(50);
  }
  throw new Error('Extension service worker CDP target did not terminate.');
}

export async function terminateExtensionWorker(context, worker) {
  const browser = context.browser();
  if (!browser) throw new Error('Chromium browser connection is unavailable.');
  const cdp = await browser.newBrowserCDPSession();
  let controlPage = null;
  let serviceWorkerCdp = null;
  let stopMethod = null;
  try {
    const { targetInfos } = await cdp.send('Target.getTargets');
    const target = targetInfos.find((candidate) => (
      candidate.type === 'service_worker' && candidate.url === worker.url()
    ));
    if (!target) throw new Error('Extension service worker CDP target was not found.');
    let stoppedNormally = false;
    try {
      const workerOrigin = extensionOrigin(worker.url());
      controlPage = await context.newPage();
      await controlPage.goto(`${workerOrigin}/manifest.json`, { waitUntil: 'commit' });
      serviceWorkerCdp = await context.newCDPSession(controlPage);
      const versions = new Map();
      serviceWorkerCdp.on('ServiceWorker.workerVersionUpdated', (event) => {
        for (const version of event.versions) versions.set(version.versionId, version);
      });
      await serviceWorkerCdp.send('ServiceWorker.enable');
      const versionDeadline = Date.now() + 2_000;
      let version = null;
      while (Date.now() < versionDeadline) {
        version = [...versions.values()].find((candidate) => (
          candidate.scriptURL === worker.url() && candidate.runningStatus === 'running'
        )) ?? null;
        if (version !== null) break;
        await delay(25);
      }
      if (version !== null) {
        await serviceWorkerCdp.send('ServiceWorker.stopWorker', {
          versionId: version.versionId,
        });
        stopMethod = 'stopWorker';
      } else {
        await serviceWorkerCdp.send('ServiceWorker.stopAllWorkers');
        stopMethod = 'stopAllWorkers';
      }
      await waitForTargetClosure(cdp, target.targetId, 3_000);
      stoppedNormally = true;
    } catch {
      // Fall back for Chromium builds that do not expose extension workers in this domain.
    }
    if (!stoppedNormally) {
      const result = await cdp.send('Target.closeTarget', { targetId: target.targetId });
      if (result.success !== true) {
        throw new Error('Chromium refused to close the service worker target.');
      }
      await waitForTargetClosure(cdp, target.targetId);
    }
    return {
      targetId: target.targetId,
      extensionOrigin: extensionOrigin(worker.url()),
      stoppedNormally,
      stopMethod,
    };
  } finally {
    await serviceWorkerCdp?.detach().catch(() => undefined);
    await controlPage?.close().catch(() => undefined);
    await cdp.detach();
  }
}

export async function startExtensionWorker(context, extensionOrigin) {
  const controlPage = await context.newPage();
  let cdp = null;
  try {
    await controlPage.goto(`${extensionOrigin}/manifest.json`, { waitUntil: 'commit' });
    cdp = await context.newCDPSession(controlPage);
    await cdp.send('ServiceWorker.enable');
    await cdp.send('ServiceWorker.startWorker', { scopeURL: `${extensionOrigin}/` });
  } finally {
    await cdp?.detach().catch(() => undefined);
    await controlPage.close().catch(() => undefined);
  }
}
