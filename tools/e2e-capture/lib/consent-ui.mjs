import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { promisify } from 'node:util';
import { randomBytes, randomUUID } from 'node:crypto';

import { expect } from '@playwright/test';
import { runNativePermissionInteraction } from './native-permission-diagnostics.mjs';
import { isTask } from '../../../shared/browser-tasks/wire.mjs';

const execFileAsync = promisify(execFile);
const syntheticLegalBindings = JSON.parse(await readFile(
  new URL('../../../extension/tests/fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

export const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';
export const LOCAL_SERVICE_ORIGIN = 'http://bridge.localhost:17871';
export const LOCAL_SERVICE_ORIGIN_PATTERN = `${LOCAL_SERVICE_ORIGIN}/*`;

export async function openPopup(context, targetExtensionId, pageErrors) {
  const previousErrorCount = pageErrors.length;
  const popup = await context.newPage();
  popup.on('pageerror', (error) => pageErrors.push(error.message));
  await popup.goto(`chrome-extension://${targetExtensionId}/popup.html`, {
    waitUntil: 'domcontentloaded',
  });
  try {
    await expect(popup.locator('#mode-label')).not.toHaveText('Checking status…');
  } catch (error) {
    if (pageErrors.length > previousErrorCount) {
      throw new Error(`Extension page failed to start: ${pageErrors.slice(previousErrorCount).join('; ')}`, { cause: error });
    }
    throw error;
  }
  return popup;
}

export async function openSetup(popup) {
  const context = popup.context();
  const base = new URL('setup.html', popup.url()).href;
  let setup = context.pages().find((page) => page.url() === base || page.url().startsWith(base + '#'));
  if (!setup) {
    const opened = context.waitForEvent('page');
    await popup.locator('#journey-primary').click();
    setup = await opened;
    await expect(setup).toHaveURL(/\/setup\.html#journey=[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u);
  }
  await setup.bringToFront();
  await expect(setup.locator('main')).toHaveAttribute('data-ready', 'true');
  return setup;
}

export async function openManageExtension(popup) {
  const context = popup.context();
  const base = new URL('options.html', popup.url()).href;
  const existing = context.pages().find((page) => page.url() === base || page.url().startsWith(base + '#'));
  const opened = existing ? Promise.resolve(existing) : context.waitForEvent('page');
  await popup.locator('#open-manage').click();
  const options = await opened;
  await expect(options.locator('#data')).toBeVisible();
  return options;
}

export async function browserProcessId(context) {
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

export async function acceptNativeHostPermissionPrompt(context, { readPermission, requestKind } = {}) {
  if (process.platform !== 'win32') {
    throw new Error('Native optional-permission automation is not configured for this platform');
  }
  const targetProcessId = await browserProcessId(context);
  const script = String.raw`
$diagnostic = [ordered]@{schema='native-permission-diagnostic/v1';stage='started';window_found=$false;prompt_found=$false;invoke_attempted=$false;allow_invoked=$false;prompt_dismissed=$false}
function Write-NativePermissionStage([string]$Stage) {
  $diagnostic.stage = $Stage
  [Console]::Out.WriteLine(($diagnostic | ConvertTo-Json -Compress))
}
Write-NativePermissionStage 'started'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$deadline = (Get-Date).AddSeconds(10)
$targetProcessId = ${targetProcessId}
do {
  # Scope UIAutomation to the browser we launched. Searching every descendant
  # of the desktop can block on unrelated applications before reaching Chrome.
  $windowHandle = (Get-Process -Id $targetProcessId).MainWindowHandle
  if ($windowHandle -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 100; continue }
  if (-not $diagnostic.window_found) { $diagnostic.window_found = $true; Write-NativePermissionStage 'window_found' }
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($windowHandle)
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
    if (-not $diagnostic.prompt_found) { $diagnostic.prompt_found = $true; Write-NativePermissionStage 'prompt_found' }
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
        if (-not $diagnostic.invoke_attempted) { $diagnostic.invoke_attempted = $true; Write-NativePermissionStage 'invoke_attempted' }
        $invoke = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
        if (-not $diagnostic.allow_invoked) { $diagnostic.allow_invoked = $true; Write-NativePermissionStage 'allow_returned' }
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
        if ($null -eq $remaining) { $diagnostic.prompt_dismissed = $true; Write-NativePermissionStage 'prompt_dismissed'; exit 0 }
      } while ((Get-Date) -lt $dismissDeadline)
      throw 'Chrome optional host permission prompt did not close after Allow'
    }
  }
  Start-Sleep -Milliseconds 100
} while ((Get-Date) -lt $deadline)
throw 'Chrome optional host permission prompt was not found'
`;
  await runNativePermissionInteraction({
    requestKind, readPermission,
    run: (signal) => execFileAsync('powershell.exe', [
      '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script,
    ], { signal, windowsHide: true, maxBuffer: 16 * 1024 }),
  });
}

export async function configureSyntheticLegalBindings(worker, popup) {
  await worker.evaluate((bindings) => {
    globalThis.__OFCA_TEST_LEGAL_RELEASE_BINDINGS__ = bindings;
  }, syntheticLegalBindings);
  await popup.reload({ waitUntil: 'domcontentloaded' });
  await expect(popup.locator('#legal-unavailable')).toBeHidden();
}

export async function completePreModeLegalActions(popup, mode = 'preview') {
  popup = await openSetup(popup);
  await expect(popup.locator('#start-choice')).toBeVisible();
  await popup.locator(mode === 'full' ? '#start-full' : '#start-preview').click();
  if (mode === 'full') {
    await expect(popup.locator('#desktop-entry')).toBeVisible();
    await admitSyntheticBrowserAccess(popup);
  }
  await expect(popup.locator('#pre-mode')).toBeVisible();
  if (mode === 'full') await assertFullProminentDisclosure(popup, { initial: true });
  else {
    const preview = popup.locator('#start-disclosure');
    await expect(preview).toContainText('It keeps local tokens');
    await expect(preview).toContainText('the key used to create them');
    await expect(preview).toContainText('today and the previous six days');
    await expect(preview).toContainText('observation code already attached to a page can still temporarily process');
  }
  await popup.locator('#terms-accepted').check();
  await expect(popup.locator('#risk-acknowledged')).toBeEnabled();
  await popup.locator('#risk-acknowledged').check();
  const activate = popup.locator('#activate-software');
  await expect(activate).toBeEnabled();
  await activate.click();
  await expect(popup.locator('#access-card')).toBeVisible();
  return popup;
}

/** Extension-consent precondition only, not proof of native/task admission.
 * The installed and desktop-owner suites qualify that separate boundary. The
 * real workspace, disclosure, user gestures and audit transactions remain in use.
 */
export async function admitSyntheticBrowserAccess(setup) {
  const record = await setup.evaluate(async () => {
    const result = await chrome.runtime.sendMessage({ type: 'ofca.workspace.v1', action: 'read' });
    if (result?.ok !== true) throw Error('fixture_workspace_unavailable');
    return result.result;
  });
  const task = { task_id: randomUUID(), journey_id: record.journey_id, operation_id: randomUUID(),
    purpose: 'browser.access', scope_digest: randomBytes(32).toString('hex'), source_revision: 0,
    admission_generation: randomUUID(), state: 'accepted', expires_at: Date.now() / 1000 + 300 };
  if (!isTask(task)) throw Error('fixture_browser_task_invalid');
  await setup.evaluate(entry => chrome.storage.session.set({ browser_task_entry_v1: entry }),
    { task, draft_scope: record.draft_scope });
}

async function finishSyntheticBrowserAccess(setup) {
  const dismissed = await setup.evaluate(async () => {
    const result = await chrome.runtime.sendMessage({ type: 'ofca.workspace.v1', action: 'dismiss_browser_task' });
    return result?.ok === true && result.result?.dismissed === true;
  });
  expect(dismissed).toBe(true);
}

async function hasOrigins(worker, origins) {
  return worker.evaluate(async (patterns) => {
    const results = await Promise.all(
      patterns.map((origin) => chrome.permissions.contains({ origins: [origin] })),
    );
    return results.every(Boolean);
  }, origins);
}

async function acceptPermissionFor(context, popup, worker, buttonName, origins, requestKind = 'full') {
  const alreadyGranted = await hasOrigins(worker, origins);
  await popup.getByRole('button', { name: buttonName }).click();
  if (!alreadyGranted) await acceptNativeHostPermissionPrompt(context, {
    requestKind,
    readPermission: () => hasOrigins(worker, origins),
  });
  for (const origin of origins) {
    await expect.poll(
      () => worker.evaluate(
        (pattern) => chrome.permissions.contains({ origins: [pattern] }),
        origin,
      ),
      { message: `The popup transition did not grant ${origin}.` },
    ).toBe(true);
  }
}

export async function enablePreviewAnalytics(context, popup, worker) {
  const setup = await openSetup(popup);
  await expect(setup.locator('#access-card')).toBeVisible();
  await expect(setup.locator('#restore-access')).toHaveText('Allow site access');
  await acceptPermissionFor(
    context,
    setup,
    worker,
    'Allow site access',
    [ONLYFANS_ORIGIN_PATTERN],
    'preview',
  );
  await expect(setup.locator('#preview-metrics')).toBeVisible();
  await popup.bringToFront();
}

export async function assertFullProminentDisclosure(popup, { initial = false } = {}) {
  const full = popup.locator(initial ? '#start-disclosure' : '#full-disclosure');
  await expect(full).toBeVisible();
  if (initial) await expect(popup.locator('#activation-title')).toHaveText('Turn on Full analytics');
  await expect(full).toContainText('Full handles and keeps message text, identifiers, participant details');
  await expect(full).toContainText('companion analytics app on the same computer');
  await expect(full).toContainText('no general automatic expiry');
  await expect(full).toContainText("Deleting extension data does not delete the companion's copies");
  await expect(full).toContainText("Enabling Full is not consent on anyone else's behalf");
  const notice = full.getByRole('link', { name: 'Extension Privacy Notice' });
  const risk = full.getByRole('link', { name: 'Creator platform and data risks' });
  await expect(notice).toBeVisible();
  await expect(risk).toBeVisible();
  await expect(notice).toHaveAttribute('href', /^https:\/\//);
  await expect(risk).toHaveAttribute('href', /^https:\/\//);
  const choices = popup.locator(initial
    ? '#start-disclosure [data-approved-copy="separate-choices"]' : '#separate-choices-disclosure');
  await expect(choices).toBeVisible();
  await expect(choices).toContainText('Enabling Full does not enable history access');
  await expect.poll(async () => {
    const [copy, action] = await Promise.all([choices.boundingBox(),
      popup.locator(initial ? '#activate-software' : '#enable-full').boundingBox()]);
    return copy !== null && action !== null && copy.y + copy.height <= action.y;
  }, { message: 'Separate choices must appear before the action' }).toBe(true);
  if (!initial) await expect(full.getByRole('button', { name: 'Enable Full analytics' })).toBeVisible();
}

export async function upgradePreviewToFull(context, popup, worker) {
  const review = popup.locator('#journey-primary');
  await expect(review).toHaveText('Review Full analytics');
  await review.click();
  const setup = await openSetup(popup);
  await expect(setup.locator('#desktop-entry')).toBeVisible();
  await admitSyntheticBrowserAccess(setup);
  await assertFullProminentDisclosure(setup);
  await acceptPermissionFor(context, setup, worker, 'Enable Full analytics', [ONLYFANS_ORIGIN_PATTERN]);
  await finishSyntheticBrowserAccess(setup);
  await popup.bringToFront();
}

export async function connectFullAnalytics(context, popup, worker) {
  await configureSyntheticLegalBindings(worker, popup);
  const setup = await completePreModeLegalActions(popup, 'full');
  await acceptPermissionFor(context, setup, worker, 'Allow site access', [ONLYFANS_ORIGIN_PATTERN]);
  await finishSyntheticBrowserAccess(setup);
  await popup.bringToFront();
}
