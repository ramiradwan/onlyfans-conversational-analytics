import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { promisify } from 'node:util';

import { expect } from '@playwright/test';

const execFileAsync = promisify(execFile);
const syntheticLegalBindings = JSON.parse(await readFile(
  new URL('../../../extension/tests/fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
  'utf8',
));

export const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';
export const LOCAL_SERVICE_ORIGIN = 'http://bridge.localhost:17871';
export const LOCAL_SERVICE_ORIGIN_PATTERN = `${LOCAL_SERVICE_ORIGIN}/*`;

export async function openPopup(context, targetExtensionId, pageErrors) {
  const popup = await context.newPage();
  popup.on('pageerror', (error) => pageErrors.push(error.message));
  await popup.goto(`chrome-extension://${targetExtensionId}/popup.html`, {
    waitUntil: 'domcontentloaded',
  });
  await expect(popup.locator('#mode-label')).not.toHaveText('Checking local status…');
  return popup;
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

export async function acceptNativeHostPermissionPrompt(context) {
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

export async function configureSyntheticLegalBindings(worker, popup) {
  await worker.evaluate((bindings) => {
    globalThis.__OFCA_TEST_LEGAL_RELEASE_BINDINGS__ = bindings;
  }, syntheticLegalBindings);
  await popup.reload({ waitUntil: 'domcontentloaded' });
  await expect(popup.locator('#legal-unavailable')).toBeHidden();
}

export async function completePreModeLegalActions(popup) {
  await expect(popup.locator('#pre-mode')).toBeVisible();
  await popup.locator('#terms-accepted').check();
  await expect(popup.locator('#risk-acknowledged')).toBeEnabled();
  await popup.locator('#risk-acknowledged').check();
  const activate = popup.getByRole('button', { name: 'Activate Software' });
  await expect(activate).toBeEnabled();
  await activate.click();
  await expect(popup.locator('#mode-choice')).toBeVisible();
}

async function acceptPermissionFor(context, popup, worker, buttonName, origins) {
  await popup.getByRole('button', { name: buttonName }).click();
  await acceptNativeHostPermissionPrompt(context);
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
  await expect(popup.locator('#preview-disclosure')).toBeVisible();
  await expect(popup.getByRole('button', { name: 'Enable Preview' })).toBeVisible();
  await acceptPermissionFor(
    context,
    popup,
    worker,
    'Enable Preview',
    [ONLYFANS_ORIGIN_PATTERN],
  );
}

export async function assertFullProminentDisclosure(popup) {
  const full = popup.locator('#full-disclosure');
  await expect(full).toBeVisible();
  await expect(full).toContainText('Full analytics handles substantially more information than Preview.');
  await expect(full).toContainText('Message content:');
  await expect(full).toContainText('leaves the Extension but remains on the same computer');
  await expect(full).toContainText('No general automatic expiry period currently applies');
  await expect(full).toContainText('Delete all Extension data does not delete conversation information already stored by the companion analytics service');
  await expect(full).toContainText('not consent on behalf of those people');
  await expect(full.getByRole('link', { name: 'Extension Privacy Notice' })).toBeVisible();
  await expect(full.getByRole('button', { name: 'Enable Full analytics' })).toBeVisible();
}

export async function upgradePreviewToFull(context, popup, worker) {
  await popup.getByRole('button', { name: 'Review Full analytics' }).click();
  await assertFullProminentDisclosure(popup);
  await acceptPermissionFor(
    context,
    popup,
    worker,
    'Enable Full analytics',
    [ONLYFANS_ORIGIN_PATTERN, LOCAL_SERVICE_ORIGIN_PATTERN],
  );
}

export async function connectFullAnalytics(context, popup, worker) {
  await configureSyntheticLegalBindings(worker, popup);
  await completePreModeLegalActions(popup);
  await assertFullProminentDisclosure(popup);
  await acceptPermissionFor(
    context,
    popup,
    worker,
    'Enable Full analytics',
    [ONLYFANS_ORIGIN_PATTERN, LOCAL_SERVICE_ORIGIN_PATTERN],
  );
}
