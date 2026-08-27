import { mkdtemp, readFile, rm } from 'node:fs/promises';
import net from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import { expect, test } from '@playwright/test';

import { SyntheticPlatform, SYNTHETIC } from '../fixtures/synthetic-platform.mjs';
import {
  extensionId,
  extensionState,
  extensionWorker,
  launchExtensionBrowser,
} from '../lib/extension-browser.mjs';
import { EXTENSION_DIST, assertBuiltExtension } from '../lib/paths.mjs';

const LOCAL_SERVICE_ORIGIN = 'http://bridge.localhost:17871';
const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';
const LOCAL_SERVICE_ORIGIN_PATTERN = `${LOCAL_SERVICE_ORIGIN}/*`;
const IDENTITY_PATH = '/api2/v2/users/me';
const CHATS_PATH = '/api2/v2/chats';
const MESSAGES_PATH = `/api2/v2/chats/${SYNTHETIC.chatId}/messages`;
const execFileAsync = promisify(execFile);

function localServiceAcceptsConnections() {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: '127.0.0.1', port: 17871 });
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(result);
    };
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.setTimeout(500, () => finish(false));
  });
}

function observeLocalServiceRequests(context) {
  const requests = [];
  const listener = (request) => {
    if (request.url().startsWith(`${LOCAL_SERVICE_ORIGIN}/`)) {
      requests.push({ method: request.method(), url: request.url() });
    }
  };
  context.on('request', listener);
  return {
    requests,
    stop() { context.off('request', listener); },
  };
}

async function blockLocalService(context) {
  await context.route(`${LOCAL_SERVICE_ORIGIN}/**`, (route) => route.abort('blockedbyclient'));
}

async function allowExtensionResources(context, targetExtensionId) {
  await context.route(
    `chrome-extension://${targetExtensionId}/**`,
    (route) => route.continue(),
  );
}

async function extensionSnapshot(worker) {
  return worker.evaluate(async () => {
    const [state, permissions, scripts, local, session, databases] = await Promise.all([
      globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__(),
      chrome.permissions.getAll(),
      chrome.scripting.getRegisteredContentScripts(),
      chrome.storage.local.get(null),
      chrome.storage.session.get(null),
      indexedDB.databases(),
    ]);
    return {
      state,
      permissions,
      scriptIds: scripts.map((entry) => entry.id).sort(),
      local,
      session,
      databaseNames: databases
        .map((entry) => entry.name)
        .filter((name) => typeof name === 'string')
        .sort(),
    };
  });
}

async function openPopup(context, targetExtensionId, pageErrors) {
  const popup = await context.newPage();
  popup.on('pageerror', (error) => pageErrors.push(error.message));
  await popup.goto(`chrome-extension://${targetExtensionId}/popup.html`, {
    waitUntil: 'domcontentloaded',
  });
  await expect(popup.locator('#mode-label')).not.toHaveText('Checking local status…');
  return popup;
}

async function createClosedExtensionDatabases(worker, names) {
  await worker.evaluate(async (databaseNames) => {
    for (const databaseName of databaseNames) {
      await new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName, 1);
        request.onupgradeneeded = () => {
          request.result.createObjectStore('synthetic');
        };
        request.onsuccess = () => {
          request.result.close();
          resolve();
        };
        request.onerror = () => reject(request.error ?? new Error('Synthetic database failed'));
      });
    }
  }, names);
}

function expectNoOptionalAccess(snapshot) {
  expect(snapshot.permissions.origins ?? []).not.toContain(ONLYFANS_ORIGIN_PATTERN);
  expect(snapshot.permissions.origins ?? []).not.toContain(LOCAL_SERVICE_ORIGIN_PATTERN);
  expect(snapshot.permissions.permissions ?? []).not.toContain('webRequest');
}

async function browserProcessId(context) {
  const cdp = await context.browser().newBrowserCDPSession();
  try {
    const { processInfo } = await cdp.send('SystemInfo.getProcessInfo');
    const browser = processInfo.find((candidate) => candidate.type === 'browser');
    if (!Number.isSafeInteger(browser?.id) || browser.id <= 0) {
      throw new Error('Chrome browser process ID is unavailable');
    }
    return browser.id;
  } finally {
    await cdp.detach();
  }
}

async function acceptNativeHostPermissionPrompt(context) {
  if (process.platform !== 'win32') {
    throw new Error('Native optional-permission automation is not configured for this platform');
  }
  const targetProcessId = await browserProcessId(context);
  const script = String.raw`
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$deadline = (Get-Date).AddSeconds(10)
$targetProcessId = ${targetProcessId}
do {
  $promptNameCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty,
    '"Conversation Analytics" has requested additional permissions.'
  )
  $promptProcessCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
    $targetProcessId
  )
  $promptCondition = New-Object System.Windows.Automation.AndCondition(
    $promptNameCondition,
    $promptProcessCondition
  )
  $prompt = $root.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    $promptCondition
  )
  if ($null -ne $prompt) {
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::NameProperty,
      'Allow'
    )
    $typeCondition = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
      [System.Windows.Automation.ControlType]::Button
    )
    $buttonCondition = New-Object System.Windows.Automation.AndCondition(
      $nameCondition,
      $typeCondition
    )
    $button = $prompt.FindFirst(
      [System.Windows.Automation.TreeScope]::Descendants,
      $buttonCondition
    )
    if ($null -ne $button -and $button.Current.IsEnabled) {
      try {
        $invoke = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
      } catch {
        Start-Sleep -Milliseconds 100
        continue
      }
      $dismissDeadline = (Get-Date).AddSeconds(5)
      do {
        Start-Sleep -Milliseconds 100
        $remaining = $root.FindFirst(
          [System.Windows.Automation.TreeScope]::Descendants,
          $promptCondition
        )
        if ($null -eq $remaining) { exit 0 }
      } while ((Get-Date) -lt $dismissDeadline)
      throw 'Chrome optional host permission prompt did not close after Allow'
    }
  }
  Start-Sleep -Milliseconds 100
} while ((Get-Date) -lt $deadline)
throw 'Chrome optional host permission prompt was not found'
`;
  await execFileAsync('powershell.exe', [
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-Command',
    script,
  ], { timeout: 15_000, windowsHide: true });
}

test('standalone preview survives pause, deletion, and restart without a local service', async () => {
  test.slow();
  assertBuiltExtension();
  expect(await localServiceAcceptsConnections()).toBe(false);

  const buildMeta = JSON.parse(await readFile(path.join(EXTENSION_DIST, 'build-meta.json'), 'utf8'));
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-standalone-preview-'));
  const browserProfile = path.join(temporaryRoot, 'browser-profile');
  const pageErrors = [];
  let context = null;
  let localServiceTraffic = null;

  try {
    context = await launchExtensionBrowser(browserProfile);
    localServiceTraffic = observeLocalServiceRequests(context);
    const platform = new SyntheticPlatform();
    await platform.install(context);
    await blockLocalService(context);

    let worker = await extensionWorker(context);
    const targetExtensionId = extensionId(worker);
    expect(targetExtensionId).toBe(buildMeta.extension_id);
    await allowExtensionResources(context, targetExtensionId);

    let popup = await openPopup(context, targetExtensionId, pageErrors);
    const platformPage = await context.newPage();
    platformPage.on('pageerror', (error) => pageErrors.push(error.message));
    await platformPage.goto('https://onlyfans.com/', { waitUntil: 'domcontentloaded' });
    expect(await context.cookies('https://onlyfans.com/')).toEqual([]);

    await test.step('a clean profile starts off with no optional access or capture scripts', async () => {
      await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
      await expect(popup.locator('#messages-count')).toHaveText('0');
      await expect(popup.locator('#chats-count')).toHaveText('0');
      await expect(popup.locator('#brain-status')).toHaveText('Not detected');
      const snapshot = await extensionSnapshot(worker);
      expect(snapshot.state.consentMode).toBe('off');
      expect(snapshot.state.capturePhase).toBe('off');
      expect(snapshot.state.runtimeReady).toBe(false);
      expect(snapshot.scriptIds).toEqual([]);
      expect(snapshot.local).toEqual({});
      expect(snapshot.session).toEqual({});
      expect(snapshot.databaseNames).toEqual([]);
      expectNoOptionalAccess(snapshot);
    });

    await test.step('the popup user action grants only site access and enables preview capture', async () => {
      await popup.getByRole('button', { name: 'Enable activity preview' }).click();
      await acceptNativeHostPermissionPrompt(context);
      await expect.poll(() => worker.evaluate(
        (origin) => chrome.permissions.contains({ origins: [origin] }),
        ONLYFANS_ORIGIN_PATTERN,
      )).toBe(true);
      await expect(popup.locator('#mode-label')).toHaveText('Activity preview enabled');
      await expect.poll(async () => (await extensionState(worker)).capturePhase).toBe('preview');
      const snapshot = await extensionSnapshot(worker);
      expect(snapshot.permissions.origins ?? []).toContain(ONLYFANS_ORIGIN_PATTERN);
      expect(snapshot.permissions.origins ?? []).not.toContain(LOCAL_SERVICE_ORIGIN_PATTERN);
      expect(snapshot.permissions.permissions ?? []).not.toContain('webRequest');
      expect(snapshot.scriptIds).toEqual(['ofca-preview-isolated', 'ofca-preview-main']);
      expect(snapshot.state.runtimeReady).toBe(false);
      await expect.poll(() => platformPage.evaluate(
        () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
      )).toBe('preview');
    });

    await test.step('synthetic read responses produce useful rolling seven-day metrics', async () => {
      await platformPage.evaluate(async (paths) => {
        for (const pathValue of paths) await globalThis.fixtureRead(pathValue);
      }, [IDENTITY_PATH, CHATS_PATH, MESSAGES_PATH]);

      await expect.poll(async () => (await extensionState(worker)).preview.message_observations)
        .toBe(3);
      await expect.poll(async () => (await extensionState(worker)).preview.chat_observations)
        .toBe(1);
      await popup.reload({ waitUntil: 'domcontentloaded' });
      await expect(popup.locator('#messages-count')).toHaveText('3');
      await expect(popup.locator('#chats-count')).toHaveText('1');
      await expect(popup.locator('#inbound-count')).toHaveText('2');
      await expect(popup.locator('#outbound-count')).toHaveText('1');

      const snapshot = await extensionSnapshot(worker);
      expect(snapshot.state.preview.retention_days).toBe(7);
      expect(snapshot.state.preview.days).toHaveLength(1);
      const stored = JSON.stringify(snapshot.local);
      for (const forbiddenValue of [
        ...SYNTHETIC.historyTexts,
        ...SYNTHETIC.historyMessageIds,
        SYNTHETIC.creatorId,
        SYNTHETIC.chatId,
      ]) expect(stored).not.toContain(forbiddenValue);
      expect(snapshot.state.runtimeReady).toBe(false);
    });

    let pausedMetrics = null;
    await test.step('pause unregisters capture and subsequent reads do not increase metrics', async () => {
      await popup.getByRole('button', { name: 'Pause analytics' }).click();
      await expect(popup.locator('#mode-label')).toHaveText('Analytics paused');
      await expect.poll(async () => (await extensionState(worker)).capturePhase).toBe('paused');
      const paused = await extensionSnapshot(worker);
      pausedMetrics = paused.state.preview;
      expect(paused.scriptIds).toEqual([]);
      await expect.poll(() => platformPage.evaluate(
        () => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode ?? null,
      )).toBeNull();

      await platformPage.evaluate(async (paths) => {
        for (const pathValue of paths) await globalThis.fixtureRead(pathValue);
      }, [CHATS_PATH, MESSAGES_PATH]);
      await new Promise((resolve) => setTimeout(resolve, 500));
      expect((await extensionSnapshot(worker)).state.preview).toEqual(pausedMetrics);
    });

    await test.step('delete all clears storage, every IndexedDB database, and optional access', async () => {
      await createClosedExtensionDatabases(worker, ['standalone-e2e-a', 'standalone-e2e-b']);
      await worker.evaluate(async () => {
        await chrome.storage.local.set({ standalone_e2e_local: true });
        await chrome.storage.session.set({ standalone_e2e_session: true });
      });
      expect((await extensionSnapshot(worker)).databaseNames).toEqual([
        'standalone-e2e-a',
        'standalone-e2e-b',
      ]);

      popup.once('dialog', (dialog) => dialog.accept());
      await popup.getByRole('button', { name: 'Delete all extension data' }).click();
      await expect(popup.locator('#feedback')).toHaveText('All local extension data was deleted.');
      await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
      const deleted = await extensionSnapshot(worker);
      expect(deleted.state.consentMode).toBe('off');
      expect(deleted.state.capturePhase).toBe('off');
      expect(deleted.state.preview.message_observations).toBe(0);
      expect(deleted.state.preview.chat_observations).toBe(0);
      expect(deleted.scriptIds).toEqual([]);
      expect(deleted.local).toEqual({});
      expect(deleted.session).toEqual({});
      expect(deleted.databaseNames).toEqual([]);
      expectNoOptionalAccess(deleted);
    });

    await test.step('a browser restart remains cleared and off', async () => {
      localServiceTraffic.stop();
      expect(localServiceTraffic.requests).toEqual([]);
      await context.close();
      context = await launchExtensionBrowser(browserProfile);
      localServiceTraffic = observeLocalServiceRequests(context);
      await blockLocalService(context);
      worker = await extensionWorker(context);
      expect(extensionId(worker)).toBe(targetExtensionId);
      popup = await openPopup(context, targetExtensionId, pageErrors);
      await expect(popup.locator('#mode-label')).toHaveText('Analytics off — no OnlyFans access');
      await expect(popup.locator('#messages-count')).toHaveText('0');
      await expect(popup.locator('#chats-count')).toHaveText('0');
      const restarted = await extensionSnapshot(worker);
      expect(restarted.state.consentMode).toBe('off');
      expect(restarted.state.capturePhase).toBe('off');
      expect(restarted.state.runtimeReady).toBe(false);
      expect(restarted.scriptIds).toEqual([]);
      expect(restarted.local).toEqual({});
      expect(restarted.session).toEqual({});
      expect(restarted.databaseNames).toEqual([]);
      expectNoOptionalAccess(restarted);
    });

    expect(await localServiceAcceptsConnections()).toBe(false);
    expect(localServiceTraffic.requests).toEqual([]);
    expect(await context.cookies('https://onlyfans.com/')).toEqual([]);
    expect(pageErrors).toEqual([]);
    platform.assertFailClosed();
  } finally {
    localServiceTraffic?.stop();
    await context?.close().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
