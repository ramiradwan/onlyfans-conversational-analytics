import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';
const calls = (page) => page.evaluate(() => window.__surfaceFixture.calls);

test('loading setup never records approvals or requests browser access', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.software_activation);
  await expect(page.locator('#terms-accepted')).not.toBeChecked();
  await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
  await expect(page.locator('#activate-software')).toBeDisabled();
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#risk-acknowledged')).toBeEnabled();
  await page.locator('#risk-acknowledged').check();
  await expect(page.locator('#activate-software')).toBeEnabled();
  await page.locator('#activate-software').click();
  await expect(page.locator('#mode-choice')).toBeVisible();
  expect((await calls(page)).some((call) => call.type === 'permission')).toBe(false);
});

test('permission refusal stays off and never submits a mode choice', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.mode_choice, permissionGranted: false });
  await page.locator('#enable-preview').click();
  await expect(page.locator('#feedback')).toContainText('Site access was not allowed');
  const messages = await calls(page);
  expect(messages.find((call) => call.type === 'permission').userGesture).toBe(true);
  expect(messages.some((call) => call.type === 'ofca.legal-activation.choose-mode')).toBe(false);
  expect(await page.evaluate(() => window.__surfaceFixture.state.mode)).toBe('off');
});

test('Preview finishes setup without pairing or desktop requests', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.mode_choice);
  await page.locator('#enable-preview').click();
  await expect(page.locator('#journey-title')).toHaveText('Preview is ready');
  await expect(page.locator('[data-step="connect"]')).toBeHidden();
  await expect(page.locator('#companion-pairing')).toBeHidden();
  expect((await calls(page)).some((call) => ['pair', 'tab'].includes(call.type))).toBe(false);
});

test('deletion is scoped and cancellation makes no deletion request', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.delete_confirmation);
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Messages already stored by the desktop app are not deleted');
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect((await calls(page)).some((call) => call.type === 'ofca.ui.delete-local-data')).toBe(false);
  await page.locator('#delete-local-data').click();
  await dialog.getByRole('button', { name: 'Delete extension data', exact: true }).click();
  await expect.poll(async () => (await calls(page)).filter((call) => call.type === 'ofca.ui.delete-local-data').length).toBe(1);
});

test('losing runtime access hides the old comparison and offers recovery', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.pairing_compare);
  await expect(page.locator('#pairing-code')).toBeVisible();
  await page.evaluate(() => { window.__surfaceFixture.change({ runtimeUnavailable: true }); window.dispatchEvent(new Event('focus')); });
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#pairing-code')).toBeHidden();
  await expect(page.locator('#retry-runtime')).toBeEnabled();
});

for (const surface of ['popup', 'options']) {
  test(`${surface} uses pause and resume commands without changing the selected mode`, async ({ page }) => {
    await renderSurfaceState(page, { ...SURFACE_STATES.preview, surface });
    await page.locator('#pause').click();
    await expect(page.locator('#mode-label')).toHaveText('Analytics paused');
    await page.locator(surface === 'popup' ? '#journey-primary' : '#pause').click();
    await expect(page.locator('#mode-label')).toHaveText('Preview on');
    expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition').map((call) => call.mode))
      .toEqual(['pause', 'resume']);
  });
}

test('setup progress names the current navigation item through agreement and mode selection', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.software_activation);
  const current = page.locator('#setup-progress [aria-current="step"]');
  await expect(current).toHaveCount(1);
  await expect(current).toHaveAttribute('data-step', 'agree');
  await page.locator('#terms-accepted').check();
  await page.locator('#risk-acknowledged').check();
  await page.locator('#activate-software').click();
  await expect(current).toHaveCount(1);
  await expect(current).toHaveAttribute('data-step', 'mode');
  await expect(page.locator('main')).not.toHaveAttribute('aria-current');
  await page.locator('#step-agree').click();
  await expect(current).toHaveAttribute('data-step', 'agree');
});

test('popup readiness follows connection loss without leaving a stale ready claim', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#ready-details')).toBeVisible();
  await expect(page.locator('#desktop-status')).toHaveText('Running');
  await expect(page.locator('#delivery-status')).toHaveText('Connected');
  await expect(page.locator('#activation-status')).toHaveText('Active');
  await expect(page.locator('#analysis-status')).toHaveText('Ready');
  await page.evaluate(() => {
    window.__surfaceFixture.change({ reachable: false });
    window.dispatchEvent(new Event('focus'));
  });
  await expect(page.locator('#desktop-status')).toHaveText('Not running');
  await expect(page.locator('#delivery-status')).toHaveText('Not connected');
  await expect(page.locator('#analysis-status')).toHaveText('Not ready');
});
