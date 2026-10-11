import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';

for (const width of [1280, 390, 320]) {
  for (const theme of ['light', 'dark']) {
    for (const name of ['start_choice', 'software_activation', 'mode_choice', 'preview_complete', 'permission_required',
      'desktop_app_needed', 'extension_connection_unknown', 'full_tab_sleeping', 'full_access_revoked',
      'pairing_required', 'pairing_failed', 'activation_required', 'setup_complete', 'runtime_unavailable']) {
      test(`${name} uses the persistent workspace at ${width}px in ${theme}`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 960 });
        await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
        await renderSurfaceState(page, { ...SURFACE_STATES[name], hash: name === 'software_activation' ? 'full' : '' });
        await expect(page.locator('.onboarding-header .product-name')).toHaveText('Conversation Analytics');
        await expect(page.locator('main')).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        const title = page.locator('main h1:visible, main h2:visible').first();
        await expect(title).toBeVisible();
        if (name === 'start_choice') {
          await expect(page.locator('#start-choice')).toBeVisible();
          await expect(page.locator('#start-preview')).toBeVisible();
          await expect(page.locator('#start-full')).toBeVisible();
          await expect(page.locator('#pre-mode')).toBeHidden();
        }
        if (name === 'preview_complete') {
          await expect(page.locator('#messages-count')).toHaveText('128');
          await expect(page.locator('#setup-progress')).toBeHidden();
        }
        const settled = await title.textContent();
        await page.screenshot({ path: info.outputPath(`${name}-${width}-${theme}.png`), fullPage: true });
        await expect(title).toHaveText(settled);
      });
    }
  }
}

for (const width of [800, 640]) {
  test(`Full review keeps consent and its action reachable at 200% zoom in ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await renderSurfaceState(page, SURFACE_STATES.start_choice);
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    const brandHeight = await page.locator('.onboarding-header .product-name').evaluate((element) => element.getBoundingClientRect().height);
    expect(brandHeight).toBeLessThan(130);
    await expect(page.locator('#start-choice')).toBeVisible();
    await page.locator('#start-full').click();
    await expect(page.locator('#pre-mode')).toBeVisible();
    await expect(page.locator('#start-disclosure')).toContainText('no general automatic age-based expiry');
    await page.locator('#terms-accepted').check();
    await page.locator('#risk-acknowledged').check();
    const action = page.locator('#activate-software');
    await action.scrollIntoViewIfNeeded();
    await expect(action).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`full-review-zoom-200-${width}.png`), fullPage: true });
  });
}

test('setup consent works by keyboard in forced colors and reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
  await renderSurfaceState(page, SURFACE_STATES.start_choice);
  const full = page.locator('#start-full');
  await full.focus();
  await full.press('Enter');
  await expect(page.locator('#pre-mode')).toBeVisible();
  const terms = page.locator('#terms-accepted');
  await terms.focus();
  await page.keyboard.press('Space');
  await expect(terms).toBeChecked();
  const risk = page.locator('#risk-acknowledged');
  await risk.focus();
  await page.keyboard.press('Space');
  await expect(risk).toBeChecked();
  const action = page.locator('#activate-software');
  await action.focus();
  await expect(action).toBeFocused();
  expect(await action.evaluate((element) => getComputedStyle(element).transitionDuration)).toBe('0s');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

for (const width of [320, 390]) {
  test(`long setup feedback and heading do not overlap the primary action at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 640 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await renderSurfaceState(page, SURFACE_STATES.activation_required);
    await page.locator('#journey-title').evaluate((element) => {
      element.textContent = 'The connection to the selected creator account and this computer could not be confirmed.';
    });
    await page.locator('#feedback').evaluate((element) => {
      element.textContent = 'The connection could not be checked. Your earlier request may still be processing. Return to the current setup page before starting another attempt.';
    });
    const action = page.locator('#journey-primary');
    await action.scrollIntoViewIfNeeded();
    await expect(action).toBeInViewport();
    const rects = await page.evaluate(() => {
      const rect = id => document.getElementById(id).getBoundingClientRect();
      return { title: rect('journey-title').toJSON(), action: rect('journey-primary').toJSON(),
        feedback: rect('feedback').toJSON(), overflow: document.documentElement.scrollWidth > window.innerWidth };
    });
    expect(rects.overflow).toBe(false);
    expect(rects.action.top).toBeGreaterThanOrEqual(rects.title.bottom);
    expect(rects.feedback.top).toBeGreaterThanOrEqual(rects.action.bottom);
  });
}

test('Preview waiting does not display a verified icon before observer attachment', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, observer: { attachment: 'checking', helper: 'none' } });
  await expect(page.locator('#journey-icon')).not.toHaveText('✓');
});

for (const width of [1280, 390, 320]) for (const theme of ['light', 'dark']) {
  test(`receiving code validation stays in its workspace at ${width}px in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 960 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true });
    await page.getByRole('button', { name: 'I have a setup code', exact: true }).click();
    await expect(page.locator('#setup-transfer-entry')).toBeVisible();
    await expect(page.locator('#setup-transfer-code')).toBeFocused();
    await page.locator('#setup-transfer-code').fill('123');
    await page.locator('#setup-transfer-submit').click();
    await expect(page.locator('#setup-transfer-status')).toHaveText('Enter the 12-character setup code.');
    await expect(page.locator('#setup-transfer-code')).toHaveAttribute('aria-invalid', 'true');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`receiving-code-${width}-${theme}.png`), fullPage: true });
    await expect(page.locator('#setup-transfer-status')).toHaveText('Enter the 12-character setup code.');
    await page.locator('#setup-transfer-back').click();
    await expect(page.locator('#setup-transfer-entry')).toBeHidden();
    await expect(page.locator('#setup-transfer-open')).toBeFocused();
  });
}

for (const name of ['software_activation', 'mode_choice', 'permission_required', 'desktop_app_needed',
  'pairing_required', 'pairing_failed', 'activation_required', 'preview_complete', 'runtime_unavailable']) {
  test(`setup-code shortcut is absent from ${name}`, async ({ page }) => {
    await renderSurfaceState(page, { ...SURFACE_STATES[name], receiving: true,
      hash: name === 'software_activation' ? 'full' : '' });
    await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
    await expect(page.locator('#setup-transfer-open')).toBeHidden();
    await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  });
}

test('a deliberate receiving entry works from Preview and Back returns focus to its current task', async ({ page }) => {
  const journey = '11111111-1111-4111-8111-111111111111';
  await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, receiving: true, hash: `journey=${journey}`,
    sessionValues: { onboarding_code_entry_v1: journey } });
  await expect(page.locator('#setup-transfer-entry')).toBeVisible();
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  expect(await page.evaluate(() => chrome.storage.session.get('onboarding_code_entry_v1'))).toEqual({});
  await page.locator('#setup-transfer-back').click();
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
  await expect(page.locator('#journey-title')).toBeFocused();
});

test('a changed underlying setup task closes code entry without reviving its shortcut', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true });
  await page.locator('#setup-transfer-open').click();
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  await page.evaluate(() => window.__surfaceFixture.change({ agreement: false, mode: 'full', reachable: false }));
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
  await expect(page.locator('#journey-title')).toBeFocused();
  await expect(page.locator('#journey-title')).toBeVisible();
});

test('code-entry focus reaches the recovery heading after an intermediate unavailable read', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true });
  await page.locator('#setup-transfer-open').click();
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: true }));
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
  await expect(page.locator('#runtime-title')).toBeFocused();
});

const position = (page) => page.evaluate(() => {
  const rect = (selector) => document.querySelector(selector).getBoundingClientRect().toJSON();
  return { rail: rect('#setup-progress'), heading: rect('main h2:focus'), height: innerHeight,
    scroll: scrollY, overflow: document.documentElement.scrollWidth > innerWidth,
    overflowNodes: [...document.querySelectorAll('body *')].filter((element) => element.getClientRects().length
      && (element.getBoundingClientRect().right > innerWidth || element.scrollWidth > element.clientWidth + 1))
      .map((element) => ({ id: element.id, className: element.className,
        right: element.getBoundingClientRect().right, width: element.getBoundingClientRect().width,
        scroll: element.scrollWidth, client: element.clientWidth })) };
});
for (const { width, textScale } of [{ width: 1280, textScale: 1 }, { width: 390, textScale: 1 },
  { width: 320, textScale: 1 }, { width: 390, textScale: 2 }, { width: 320, textScale: 2 }]) {
  for (const theme of ['light', 'dark']) {
    test(`deliberate consent and connection transitions reveal the task at ${width}px, ${textScale * 100}% text, ${theme}`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: width === 1280 ? 640 : 960 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
      await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, permissionGranted: false, reachable: false });
      if (textScale > 1) await page.evaluate((scale) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * scale}px`;
      }, textScale);
      await page.locator('#start-full').click();
      await expect(page.locator('#activation-title')).toBeFocused();
      await page.locator('#terms-accepted').check();
      await page.locator('#risk-acknowledged').check();
      const consent = page.locator('#activate-software');
      await consent.scrollIntoViewIfNeeded();
      await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
      expect(await page.evaluate(() => scrollY)).toBeGreaterThan(100);
      await consent.click();
      await expect(page.locator('#access-title')).toBeFocused();
      const access = await position(page);
      expect(access.rail.top).toBeGreaterThanOrEqual(-1);
      expect(access.rail.bottom).toBeLessThanOrEqual(access.height);
      expect(access.heading.top).toBeGreaterThanOrEqual(0);
      expect(access.heading.bottom).toBeLessThanOrEqual(access.height);
      expect(access.overflow, JSON.stringify(access.overflowNodes)).toBe(false);
      await expect(page.locator('#access-card .back-button')).toHaveCount(0);
      await page.evaluate(() => window.__surfaceFixture.change({ permissionGranted: true }));
      await page.locator('#restore-access').click();
      await expect(page.locator('#journey-title')).toBeFocused();
      const connection = await position(page);
      expect(connection.rail.top).toBeGreaterThanOrEqual(-1);
      expect(connection.rail.bottom).toBeLessThanOrEqual(connection.height);
      expect(connection.heading.top).toBeGreaterThanOrEqual(0);
      expect(connection.heading.bottom).toBeLessThanOrEqual(connection.height);
      expect(connection.overflow).toBe(false);
      await page.screenshot({ path: info.outputPath(`task-transition-${width}-${textScale}-${theme}.png`), fullPage: true });
    });
  }
}

test('validation focus and passive updates keep the current reading position', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await renderSurfaceState(page, SURFACE_STATES.start_choice);
  await page.locator('#start-full').click();
  await page.locator('#activate-software').click();
  await expect(page.locator('#terms-accepted')).toBeFocused();
  await page.locator('#terms-accepted').check();
  await page.locator('#activate-software').click();
  await expect(page.locator('#risk-acknowledged')).toBeFocused();
  const before = await page.evaluate(() => scrollY);
  await page.evaluate(() => window.__surfaceFixture.change({ preview: { message_observations: 129 } }));
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('#risk-acknowledged')).toBeFocused();
  expect(await page.evaluate(() => scrollY)).toBe(before);
});

test('a pending action cannot position or focus a page restored after pagehide', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await renderSurfaceState(page, SURFACE_STATES.start_choice);
  await page.evaluate(() => {
    const originalSend = chrome.runtime.sendMessage;
    let held = false;
    chrome.runtime.sendMessage = async (message) => {
      if (message.type === 'ofca.ui.status' && !held) {
        held = true;
        await new Promise((resolve) => { window.__releaseActionRead = resolve; });
      }
      return originalSend(message);
    };
  });
  await page.locator('#start-full').click();
  await page.waitForFunction(() => typeof window.__releaseActionRead === 'function');
  await expect(page.locator('#pre-mode')).toBeVisible();
  await expect(page.locator('main')).toHaveAttribute('aria-busy', 'true');
  await page.evaluate(() => {
    document.body.setAttribute('tabindex', '-1'); document.body.focus({ preventScroll: true });
    scrollTo(0, document.documentElement.scrollHeight);
    window.__restoredTaskScrolls = 0; window.__restoredTaskFocus = 0;
    const originalScroll = Element.prototype.scrollIntoView, originalFocus = HTMLElement.prototype.focus;
    Element.prototype.scrollIntoView = function (...values) {
      if (this.matches('.onboarding-layout')) window.__restoredTaskScrolls++;
      return originalScroll.apply(this, values);
    };
    HTMLElement.prototype.focus = function (...values) {
      if (this.id === 'activation-title') window.__restoredTaskFocus++;
      return originalFocus.apply(this, values);
    };
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    window.__releaseActionRead();
  });
  await expect(page.locator('main')).toHaveAttribute('aria-busy', 'false');
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await page.evaluate(() => [window.__restoredTaskScrolls, window.__restoredTaskFocus])).toEqual([0, 0]);
  await expect(page.locator('#activation-title')).not.toBeFocused();
});
