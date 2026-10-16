import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';
const calls = (page) => page.evaluate(() => window.__surfaceFixture.calls.filter(call => !(call.type === 'ofca.workspace.v1' && ['read', 'draft', 'browser_task', 'desktop_adoption', 'dismiss_browser_task'].includes(call.action)) && !(call.type === 'ofca.setup-transfer.v1' && call.action === 'context')));

test('Preview keeps its status and Full review action visible in the reserved frames', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.preview);
  await expect(page.locator('#journey-title')).toBeVisible();
  await expect(page.locator('#journey-title')).toHaveText('Ready');
  await expect(page.locator('#journey-primary')).toBeVisible();
  await page.locator('#journey-primary').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.open-surface'))
    .toEqual([{ type: 'ofca.ui.open-surface', surface: 'setup', section: 'full' }]);
});

test('loading setup never records approvals or requests browser access', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, permissionGranted: false });
  await expect(page.locator('#start-choice')).toBeVisible();
  await page.locator('#start-preview').click();
  await expect(page.locator('#terms-accepted')).not.toBeChecked();
  await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
  await expect(page.locator('#activate-software')).toBeEnabled();
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
  await page.locator('#activate-software').click();
  await expect(page.locator('#feedback')).toBeVisible();
  await expect(page.locator('#terms-accepted')).toBeFocused();
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#risk-acknowledged')).toBeEnabled();
  await page.locator('#activate-software').click();
  await expect(page.locator('#risk-acknowledged')).toBeFocused();
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
  await page.locator('#risk-acknowledged').check();
  await expect(page.locator('#activate-software')).toBeEnabled();
  await page.locator('#activate-software').click();
  await expect(page.locator('#access-card')).toBeVisible();
  expect((await calls(page)).filter((call) => !call.type.endsWith('.status')).map((call) => call.type))
    .toEqual(['ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software']);
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

test('Preview counts return after required review or site access without changing consent', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.preview_complete);
  const counts = page.locator('#preview-metrics');
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  await page.locator('#journey-primary').click();
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(counts).toBeHidden();
  await page.locator('#desktop-back').click();
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  await page.evaluate(() => window.__surfaceFixture.change({ phase: 'permission_required' }));
  await expect(page.locator('#access-card')).toBeVisible();
  await expect(counts).toBeHidden();
  await page.evaluate(() => window.__surfaceFixture.change({ phase: 'preview' }));
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  expect(await page.evaluate(() => window.__surfaceFixture.state.mode)).toBe('preview');
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
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

test('losing runtime access hides the desktop handoff and offers recovery', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.pairing_compare);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => { window.__surfaceFixture.change({ runtimeUnavailable: true }); window.dispatchEvent(new Event('focus')); });
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#runtime-title')).toHaveText('Extension status is unavailable');
  await expect(page.locator('#desktop-entry')).toBeHidden();
  await expect(page.locator('#retry-runtime')).toBeEnabled();
  await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: false }));
  await expect(page.locator('#runtime-unavailable')).toBeHidden();
});

test('status badge and next action reflect a confirmed sleeping browser tab', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.full_tab_sleeping, surface: 'popup' });
  await expect(page.locator('#journey-title')).toHaveText('Browser tab paused');
  await expect(page.locator('#journey-badge')).toHaveText('Browser tab paused');
  await expect(page.locator('#analysis-status')).not.toHaveText('Ready');
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
});

test('unknown extension connection does not ask for another creator sign-in', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.extension_connection_unknown, surface: 'popup' });
  await expect(page.locator('#journey-title')).toHaveText('Status unavailable');
  await expect(page.locator('#journey-badge')).toHaveText('Status unavailable');
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
  await expect(page.locator('#journey-card')).not.toContainText('Sign in to your creator account');
});

test('consent revocation shows analytics off without restoring it automatically', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_access_revoked);
  await expect(page.locator('#journey-title')).toHaveText('Analytics off');
  await expect(page.locator('#journey-badge')).toHaveText('Off');
  await expect(page.locator('#journey-icon')).not.toHaveText('✓');
  await expect(page.locator('#journey-primary')).toBeHidden();
  expect((await calls(page)).some(call => call.type === 'permission')).toBe(false);
});

test('refused permission preserves the blocked state and never claims readiness', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.permission_required, permissionGranted: false });
  await expect(page.locator('#access-card')).toBeVisible();
  await page.locator('#restore-access').click();
  await expect(page.locator('#feedback')).toContainText('Site access was not allowed');
  await expect(page.locator('#journey-badge')).toHaveText('Needs access');
  expect((await calls(page)).some((call) => call.type === 'ofca.ui.transition')).toBe(false);
});

test('Full handoff keeps one nonvisual status announcement as runtime facts change', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_required);
  const announcement = page.locator('#step-summary');
  await expect(announcement).toHaveAttribute('role', 'status');
  await expect(announcement).toHaveAttribute('aria-live', 'polite');
  await expect(announcement).toHaveAttribute('aria-atomic', 'true');
  await expect(announcement).toHaveClass(/visually-hidden/);
  await expect(announcement).toHaveText('Continue in the desktop app');
  await page.evaluate(() => {
    window.__announcementChanges = 0;
    new MutationObserver(() => { window.__announcementChanges++; })
      .observe(document.querySelector('#step-summary'), { childList: true, characterData: true, subtree: true });
    window.__surfaceFixture.change({ commercial: 'active', ready: true });
  });
  await expect(announcement).toHaveText('Continue in the desktop app');
  await expect(page.locator('#desktop-entry')).toBeVisible();
  const initial = await page.evaluate(() => window.__announcementChanges);
  await page.evaluate(() => window.__surfaceFixture.change({}));
  await expect.poll(async () => page.evaluate(() => window.__announcementChanges)).toBe(initial);
});

test('popup announces a changed connection status only once', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  const announcement = page.locator('#popup-status-announcement');
  await expect(announcement).toHaveAttribute('role', 'status');
  await expect(announcement).toHaveAttribute('aria-live', 'polite');
  await expect(announcement).toHaveAttribute('aria-atomic', 'true');
  await expect(announcement).toHaveText(/Ready/);
  await page.evaluate(() => {
    window.__popupAnnouncements = 0;
    new MutationObserver(() => { window.__popupAnnouncements++; })
      .observe(document.querySelector('#popup-status-announcement'), { childList: true, characterData: true, subtree: true });
    window.__surfaceFixture.change({ reachable: false });
  });
  await expect(announcement).toContainText('Not connected');
  const changes = await page.evaluate(() => window.__popupAnnouncements);
  await page.evaluate(() => window.__surfaceFixture.change({}));
  await expect.poll(async () => page.evaluate(() => window.__popupAnnouncements)).toBe(changes);
});

test('popup reports an unavailable status without claiming reconnection is still running', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.runtime_unavailable, surface: 'popup' });
  await expect(page.locator('#runtime-title')).toHaveText('Extension status is unavailable');
  await expect(page.locator('#retry-runtime')).toBeEnabled();
});

test('popup uses pause and resume commands without changing the selected mode', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview, surface: 'popup' });
  await page.locator('#pause').click();
  await expect(page.locator('#mode-label')).toHaveText('Analytics paused');
  await page.locator('#journey-primary').click();
  await expect(page.locator('#mode-label')).toHaveText('Preview on');
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition').map((call) => call.mode))
    .toEqual(['pause', 'resume']);
});

for (const [name, state] of Object.entries(SURFACE_STATES).filter(([, value]) => value.surface === 'popup')) {
  test(`popup keyboard order follows its content layout (${name})`, async ({ page }) => {
    await renderSurfaceState(page, state);
    await expect(page.locator('#journey-primary')).toBeVisible();
    const stops = [];
    for (let index = 0; index < 12; index++) {
      await page.keyboard.press('Tab');
      const stop = await page.evaluate(() => {
        const node = document.activeElement;
        if (!node || node === document.body) return null;
        const content = node.closest('.popup-content > [data-region-content]');
        return { key: node.id || node.outerHTML, top: content ? node.getBoundingClientRect().top - content.getBoundingClientRect().top : null };
      });
      if (!stop || stops.some((seen) => seen.key === stop.key)) break;
      stops.push(stop);
    }
    const tops = stops.filter((stop) => stop.top !== null).map((stop) => stop.top);
    expect(tops.length).toBeGreaterThan(0);
    expect(tops).toEqual([...tops].sort((a, b) => a - b));
  });
}

test('while the desktop app controls this browser, the popup explains where pause moved', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.desktop_controlled);
  await expect(page.locator('#pause')).toBeHidden();
  await expect(page.locator('#desktop-control-note')).toBeVisible();
});

test('the popup shows no desktop pause note while it owns pause itself', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#desktop-control-note')).toBeHidden();
});

test('persistent setup sends paused Full to desktop control instead of duplicating Resume', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.desktop_controlled_paused, surface: 'setup' });
  await expect(page.locator('#pause')).toBeHidden();
  await expect(page.locator('#open-desktop')).toHaveText('Open desktop app');
  await page.locator('#open-desktop').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition')).toEqual([]);
});

test('paused Preview keeps Resume in the extension even with an existing desktop channel', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.desktop_controlled_paused, surface: 'setup', resume: 'preview' });
  await expect(page.locator('#journey-primary')).toHaveText('Resume');
  await page.locator('#journey-primary').click();
  await expect.poll(async () => (await calls(page)).filter((call) => call.type === 'ofca.ui.transition').length).toBe(1);
});

test('the compact setup window for the desktop app shows only its task', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.pairing_required, browserTask: true });
  await expect(page.locator('#journey-title')).toHaveText('Browser access is ready');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'browser-access-ready');
  await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
});

test('while the desktop app controls a paused browser, resume points to the desktop app', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.desktop_controlled_paused);
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
  await page.locator('#journey-primary').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition')).toEqual([]);
  expect((await calls(page)).filter(call => call.type === 'ofca.ui.open-surface')).toEqual([{ type: 'ofca.ui.open-surface', surface: 'setup', section: 'full' }]);
});

for (const [name, visible] of [['connection', true], ['connection_desktop_controlled', false]]) {
  test(`options ${visible ? 'offers' : 'leaves to the desktop app'} forgetting the connection (${name})`, async ({ page }) => {
    await renderSurfaceState(page, SURFACE_STATES[name]);
    await expect(page.locator('#forget-companion')).toBeVisible({ visible });
  });
}

test('history recovery does not promise collection is ready after waking the tab', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.connection, sleeping: true });
  await expect(page.locator('#history-health')).toHaveText('Your browser paused OnlyFans. Open the tab to continue.');
  await expect(page.locator('#history-health')).not.toContainText('automatically');
});

test('options offers no second pause control', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview, surface: 'options' });
  await expect(page.locator('#pause')).toHaveCount(0);
});

for (const [name, state] of [['connection', SURFACE_STATES.connection], ['manage', SURFACE_STATES.manage],
  ['permission_required', { ...SURFACE_STATES.connection, phase: 'permission_required' }]]) {
  test(`options frames show their whole cards (${name})`, async ({ page }) => {
    await renderSurfaceState(page, state);
    await expect(page.locator('#restore-access')).toBeVisible({ visible: name === 'permission_required' });
    for (const width of [1280, 601, 600, 390, 360, 359, 320]) {
      await page.setViewportSize({ width, height: 900 });
      const cut = await page.locator('.options-frame').evaluateAll((frames) => frames.map((frame) => frame.scrollHeight - frame.clientHeight));
      expect(cut, `${width}px`).toEqual([0, 0, 0]);
    }
  });
}

for (const width of [1280, 390, 320]) {
  test(`options notice band holds the loading line at ${width}px and the frames keep their place`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const rendering = renderSurfaceState(page, { ...SURFACE_STATES.manage, initialRequestHold: 'ofca.ui.status' });
    await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
    await expect(page.locator('#loading')).toBeVisible();
    const band = await page.locator('.reserved-notice').boundingBox();
    const loading = await page.locator('#loading').boundingBox();
    expect(Math.abs(loading.y - band.y)).toBeLessThan(0.5);
    expect(Math.abs(loading.height - band.height)).toBeLessThan(0.5);
    const frameTops = () => page.locator('.options-frame').evaluateAll((frames) => frames.map((frame) => frame.getBoundingClientRect().top));
    const before = await frameTops();
    await page.evaluate(() => window.__releaseInitialRequest());
    await rendering;
    await expect(page.locator('#loading')).toBeHidden();
    expect(await frameTops()).toEqual(before);
  });
}

test('only the admitted task presents Full consent and permission actions', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, browserTask: true });
  await expect(page.locator('#setup-progress')).toHaveCount(0);
  await expect(page.locator('#pre-mode')).toBeVisible();
  await page.locator('#terms-accepted').check();
  await page.locator('#risk-acknowledged').check();
  await page.locator('#activate-software').click();
  await expect(page.locator('#journey-title')).toHaveText('Browser access is ready');
  await expect(page.locator('#pairing-code')).toHaveCount(0);
  expect((await calls(page)).some(call => call.type === 'pair')).toBe(false);
});

test('activation changes leave Full continuation in the desktop app', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_required);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => window.__surfaceFixture.change({ commercial: 'active', ready: true }));
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(page.locator('#open-desktop')).toBeEnabled();
  await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
  expect((await calls(page)).some(call => call.type === 'retry_readiness')).toBe(false);
});

test('blocked analysis offers the desktop app without a competing repair workflow', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_active);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(page.locator('#open-desktop')).toBeEnabled();
  await expect(page.locator('#journey-card')).toBeHidden();
});

test('popup readiness follows connection loss without leaving a stale ready claim', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#ready-details')).toBeVisible();
  await expect(page.locator('#desktop-status')).toHaveText('Connected');
  await expect(page.locator('#delivery-status')).toHaveText('Connected');
  await expect(page.locator('#activation-status')).toHaveText('Active');
  await expect(page.locator('#analysis-status')).toHaveText('Ready');
  await page.evaluate(() => {
    window.__surfaceFixture.change({ reachable: false });
    window.dispatchEvent(new Event('focus'));
  });
  await expect(page.locator('#desktop-status')).toHaveText('Not connected');
  await expect(page.locator('#delivery-status')).toHaveText('Not connected');
  await expect(page.locator('#analysis-status')).toHaveText('Not ready');
});
