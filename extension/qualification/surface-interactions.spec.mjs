import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';
import { readFileSync } from 'node:fs';
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

for (const width of [320, 390, 600]) {
  for (const scale of [1.25, 1.5, 2]) {
    test(`connection details status slots stay fixed at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: '' });
      await page.evaluate(async (factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        await document.fonts.ready;
        const sendMessage = chrome.runtime.sendMessage;
        window.__dialogDelivery = {};
        chrome.runtime.sendMessage = async (message) => {
          const response = await sendMessage(message);
          if (message.type === 'ofca.ui.status') Object.assign(response.status.delivery, window.__dialogDelivery);
          return response;
        };
      }, scale);
      await page.locator('#open-connection-details').click();
      const dialog = page.locator('#connection-details-dialog');
      const boxes = () => dialog.evaluate((node) => [node, ...node.querySelectorAll('h2, dl, dt, dd, p, form, button')]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return [rect.x, rect.y, rect.width, rect.height];
        }));
      const initial = await boxes();
      const captureStates = [
        [{ startup_error_code: null, capture_drop_counts: {} }, 'No capture issues reported.'],
        [{ startup_error_code: null, capture_drop_counts: { unavailable: Number.MAX_SAFE_INTEGER } },
          `${new Intl.NumberFormat('en-US').format(Number.MAX_SAFE_INTEGER)} updates could not be recorded.`],
        [{ startup_error_code: 'startup_failed', capture_drop_counts: {} },
          'Full analytics could not start. Open the desktop app, then retry in setup.'],
      ];
      const historyStates = [
        [{ browser_tab_sleeping: false, history_error_code: null }, ''],
        [{ browser_tab_sleeping: false, history_error_code: 'history_unavailable' }, 'Message history needs attention in the desktop app.'],
        [{ browser_tab_sleeping: true, history_error_code: 'history_unavailable' }, 'Your browser paused OnlyFans. Open the tab to continue.'],
      ];
      for (const [capture, captureText] of captureStates) for (const [history, historyText] of historyStates) {
        for (const pending of [0, 999, Number.MAX_SAFE_INTEGER]) {
          await page.evaluate((delivery) => {
            window.__dialogDelivery = delivery;
            window.__surfaceFixture.change({});
          }, { ...capture, ...history, pending_entries: pending });
          await expect(page.locator('#capture-health')).toHaveText(captureText);
          await expect(page.locator('#history-health')).toHaveText(historyText);
          await expect(page.locator('#pending-count')).toHaveText(new Intl.NumberFormat('en-US').format(pending));
          expect.soft(await boxes(), JSON.stringify({ capture, history, pending })).toEqual(initial);
        }
      }
      await page.evaluate(() => {
        window.__dialogDelivery = {};
        window.__surfaceFixture.change({});
      });
      await expect(page.locator('#capture-health')).toHaveText('No capture issues reported.');
      await expect(page.locator('#history-health')).toBeEmpty();
      await expect(page.locator('#pending-count')).toHaveText('0');
      expect(await boxes(), 'cleared status').toEqual(initial);
      await dialog.locator('button').scrollIntoViewIfNeeded();
      const close = await dialog.locator('button').boundingBox();
      expect(close.y).toBeGreaterThanOrEqual(0);
      expect(close.y + close.height).toBeLessThanOrEqual(641);
      await dialog.locator('button').click();
      await expect(dialog).not.toBeVisible();
    });

    test(`options state slots stay fixed at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      const rendering = renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: '', initialRequestHold: 'ofca.ui.status' });
      await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
      await page.evaluate(async (factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        await document.fonts.ready;
      }, scale);
      const boxes = () => page.evaluate(() => Object.fromEntries([
        ...document.querySelectorAll('.options-frame, .reserved-status, footer'),
        ...document.querySelectorAll('.options-frame h2, .options-frame h3, .options-frame .settings-row, #open-dashboard, #open-connection-details'),
      ].map((node, index) => {
        const rect = node.getBoundingClientRect();
        return [node.id || `${node.className}:${index}`, [rect.x, rect.y + window.scrollY, rect.width, rect.height]];
      })));
      const loading = await boxes();
      for (const id of ['capture', 'connection', 'data']) await expect(page.locator(`#${id}`)).toBeHidden();
      await page.evaluate(() => window.__releaseInitialRequest());
      await rendering;
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      const loaded = await boxes();
      expect.soft(loaded, 'loading to loaded').toEqual(loading);
      await page.evaluate(() => window.__surfaceFixture.change({ reachable: false }));
      await expect(page.locator('#delivery-status')).toHaveText('Not connected');
      expect.soft(await boxes(), 'connection loss').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ reachable: true }));
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      expect.soft(await boxes(), 'reconnection').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: true }));
      await expect(page.locator('#runtime-unavailable')).toBeVisible();
      expect.soft(await boxes(), 'runtime unavailable').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: false }));
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      expect.soft(await boxes(), 'runtime recovered').toEqual(loaded);
    });

    for (const surface of ['popup', 'setup', 'options']) {
      test(`${surface} real feedback slots stay fixed at ${width}px and ${scale * 100}% text`, async ({ page }) => {
        await page.setViewportSize({ width, height: 640 });
        await renderSurfaceState(page, surface === 'popup' ? SURFACE_STATES.preview
          : surface === 'setup' ? SURFACE_STATES.activation_required : SURFACE_STATES.manage);
        await page.evaluate((factor) => {
          document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        }, scale);
        const sources = ['../ui/actions.mjs', '../ui/surface-client.mjs', '../setup.js']
          .map((name) => readFileSync(new URL(name, import.meta.url), 'utf8')).join('\n');
        const messages = [...sources.matchAll(/new NoticeError\('([^']+)'\)/gu)].map((match) => match[1]);
        messages.push('That did not complete. Try again.', 'The desktop connection has been removed.', 'Check both boxes to continue.');
        const layout = () => page.evaluate(() => {
          const slot = document.querySelector('.reserved-status');
          const following = document.querySelector('nav.menu') || document.querySelector('footer');
          const rect = following.getBoundingClientRect();
          const feedback = slot.querySelector('.feedback');
          return { slot: slot.getBoundingClientRect().toJSON(), feedback: feedback.getBoundingClientRect().toJSON(),
            following: [rect.x, rect.y + window.scrollY, rect.width, rect.height] };
        });
        const empty = await layout();
        for (const [index, message] of messages.entries()) {
          await page.locator('#feedback').evaluate((node, value) => { node.textContent = value; }, message);
          const filled = await layout();
          expect.soft(filled.following, `message ${index}`).toEqual(empty.following);
          expect.soft(filled.slot.height, `message ${index}`).toBe(empty.slot.height);
          expect.soft(filled.feedback.bottom, `message ${index}`).toBeLessThanOrEqual(filled.slot.bottom + 1);
          await page.locator('#feedback').evaluate((node) => { node.textContent = ''; });
          expect.soft((await layout()).following, `cleared ${index}`).toEqual(empty.following);
        }
      });
    }

    test(`options controls use page scrolling at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await renderSurfaceState(page, SURFACE_STATES.connection);
      await page.evaluate((factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
      }, scale);
      const frames = page.locator('.options-frame');
      const frameLayout = await frames.evaluateAll((elements) => elements.map((frame) => ({
        overflow: getComputedStyle(frame).overflowY,
        excess: frame.scrollHeight - frame.clientHeight,
      })));
      expect(frameLayout, `${width}px at ${scale * 100}%`).toEqual([
        { overflow: 'auto', excess: 0 },
        { overflow: 'auto', excess: 0 },
        { overflow: 'auto', excess: 0 },
      ]);
      const unfilled = await frames.evaluateAll((elements) => elements.map((frame) =>
        frame.getBoundingClientRect().height - parseFloat(getComputedStyle(frame).paddingBottom)
          - frame.querySelector(':scope > section').getBoundingClientRect().height));
      expect(unfilled.every((gap) => Math.abs(gap) < 1), `${width}px at ${scale * 100}%: ${unfilled}`).toBe(true);
      const controls = frames.locator('button:visible');
      expect(await controls.count()).toBeGreaterThan(0);
      for (const control of await controls.all()) {
        await control.scrollIntoViewIfNeeded();
        const bounds = await control.boundingBox();
        expect(bounds.y, await control.getAttribute('id')).toBeGreaterThanOrEqual(0);
        expect(bounds.y + bounds.height, await control.getAttribute('id')).toBeLessThanOrEqual(641);
      }
      expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
      const frameTops = () => frames.evaluateAll((elements) => elements.map((frame) => frame.getBoundingClientRect().top + window.scrollY));
      const before = await frameTops();
      await page.evaluate(() => window.__surfaceFixture.change({ phase: 'permission_required' }));
      const restoreAccess = page.locator('#restore-access');
      await expect(restoreAccess).toBeVisible();
      expect(await frameTops()).toEqual(before);
      await restoreAccess.scrollIntoViewIfNeeded();
      const restoreBounds = await restoreAccess.boundingBox();
      expect(restoreBounds.y).toBeGreaterThanOrEqual(0);
      expect(restoreBounds.y + restoreBounds.height).toBeLessThanOrEqual(641);
    });

    for (const surface of ['popup', 'setup', 'options']) {
      test(`${surface} feedback keeps its following content in place at ${width}px and ${scale * 100}% text`, async ({ page }) => {
        await page.setViewportSize({ width, height: 640 });
        const state = surface === 'popup' ? SURFACE_STATES.preview
          : surface === 'setup' ? SURFACE_STATES.activation_required : SURFACE_STATES.manage;
        await renderSurfaceState(page, state);
        await page.evaluate((factor) => {
          document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        }, scale);
        const layout = () => page.evaluate(() => {
          const status = document.querySelector('.reserved-status');
          const feedback = status.querySelector('.feedback');
          const following = document.querySelector('body[data-surface="popup"] nav.menu')
            || document.querySelector('body[data-surface="options"] footer')
            || document.querySelector('body[data-surface="setup"] footer');
          return {
            status: status.getBoundingClientRect().toJSON(),
            feedback: feedback.getBoundingClientRect().toJSON(),
            following: following.getBoundingClientRect().toJSON(),
            lineHeight: parseFloat(getComputedStyle(feedback).lineHeight),
            fontSize: parseFloat(getComputedStyle(feedback).fontSize),
          };
        });
        const empty = await layout();
        await page.locator('#feedback').evaluate((element) => { element.innerHTML = 'Status ready.<br>Try again.'; });
        const filled = await layout();
        expect(filled.lineHeight).toBeGreaterThanOrEqual(filled.fontSize * 1.4);
        expect(filled.feedback.height).toBeGreaterThanOrEqual(filled.lineHeight * 2 - 1);
        expect(filled.status.bottom).toBeGreaterThanOrEqual(filled.feedback.bottom - 1);
        expect(filled.following.top).toBeGreaterThanOrEqual(filled.feedback.bottom - 1);
        expect(filled.following.top).toBeCloseTo(empty.following.top, 0);
      });
    }
  }
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
