import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';

for (const request of ['ofca.ui.status', 'ofca.legal-activation.status']) {
  test(`initial ${request} delay keeps setup status visible until the Full task is ready`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 960 });
    const rendering = renderSurfaceState(page, { ...SURFACE_STATES.mode_choice_full, initialRequestHold: request });
    await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
    await expect(page.locator('#loading')).toBeVisible();
    await expect(page.locator('#loading')).toHaveText('Checking setup…');
    await expect(page.locator('#loading')).toHaveAttribute('role', 'status');
    await expect(page.locator('main')).toBeHidden();
    await expect(page.locator('.onboarding-footer')).toBeHidden();
    const header = await page.locator('.onboarding-header').boundingBox();
    await page.evaluate(() => window.__releaseInitialRequest());
    await rendering;
    await expect(page.locator('main')).toBeVisible();
    await expect(page.locator('#full-choice-title')).toBeVisible();
    await expect(page.locator('#setup-progress')).toHaveCount(0);
    await expect(page.locator('#loading')).toBeHidden();
    expect(await page.locator('.onboarding-header').boundingBox()).toEqual(header);
    await expect(page.locator('main')).toHaveAttribute('data-task', 'mode:full');
    await page.locator('#full-secondary').click();
    await expect(page.locator('#preview-choice-title')).toBeVisible();
    await expect(page.locator('#setup-progress')).toBeHidden();
    await expect(page.locator('main')).toHaveAttribute('data-task', 'mode:false');
  });
}

for (const name of ['legal_unavailable', 'runtime_unavailable']) {
  test(`initial ${name} reveals recovery instead of keeping the task hidden`, async ({ page }) => {
    await renderSurfaceState(page, SURFACE_STATES[name]);
    await expect(page.locator('main')).toBeVisible();
    await expect(page.locator('.onboarding-footer')).toBeVisible();
    await expect(page.locator('#loading')).toBeHidden();
    await expect(page.locator(name === 'legal_unavailable' ? '#legal-unavailable' : '#runtime-unavailable')).toBeVisible();
  });
}

test('a direct receiving entry focuses its code only after initial legal information is ready', async ({ page }) => {
  const journey = '11111111-1111-4111-8111-111111111111';
  const rendering = renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, receiving: true,
    hash: `journey=${journey}`, initialRequestHold: 'ofca.legal-activation.status',
    sessionValues: { onboarding_code_entry_v1: journey } });
  await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
  await expect(page.locator('#loading')).toBeVisible();
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-code')).not.toBeFocused();
  await page.evaluate(() => window.__releaseInitialRequest());
  await rendering;
  await expect(page.locator('#setup-transfer-entry')).toBeVisible();
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  await expect(page.locator('#loading')).toBeHidden();
});

for (const [state, card, task] of [
  [{ runtimeUnavailable: true }, '#runtime-unavailable', 'unavailable'],
  [{ configured: false }, '#legal-unavailable', 'legal-unavailable'],
]) {
  test(`Full handoff reports ${task} and returns after recovery`, async ({ page }) => {
    await renderSurfaceState(page, SURFACE_STATES.desktop_app_needed);
    await expect(page.locator('#setup-progress')).toHaveCount(0);
    await expect(page.locator('main')).toHaveAttribute('data-requirement', 'desktop-handoff');
    await page.evaluate((next) => window.__surfaceFixture.change(next), state);
    await expect(page.locator(card)).toBeVisible();
    await expect(page.locator('#setup-progress')).toBeHidden();
    await expect(page.locator('#desktop-entry')).toBeHidden();
    await expect(page.locator('main')).toHaveAttribute('data-task', task);
    await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: false, configured: true }));
    await expect(page.locator('#desktop-entry')).toBeVisible();
    await expect(page.locator('#setup-progress')).toHaveCount(0);
    await expect(page.locator('main')).toHaveAttribute('data-requirement', 'desktop-handoff');
  });
}

test('updated consent identifies a new review while preserving the exact task and disclosure content', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, browserTask: true });
  await expect(page.locator('main')).toHaveAttribute('data-task', 'agree:full');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'agreement:initial:full');
  await expect(page.locator('#review-notice')).toBeHidden();
  const disclosure = await page.locator('#start-disclosure').textContent();
  await page.locator('#terms-accepted').focus();
  await page.evaluate(() => window.__surfaceFixture.change({ mode: 'paused', resume: 'full', reauthorization: true }));
  await expect(page.locator('#review-notice')).toBeVisible();
  await expect(page.locator('main')).toHaveAttribute('data-task', 'agree:full');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'agreement:reauthorization:full');
  await expect(page.locator('#terms-accepted')).toBeFocused();
  expect(await page.locator('#start-disclosure').textContent()).toBe(disclosure);
});

for (const theme of ['light', 'dark']) {
  test(`Full handoff remains stable at 320px and 200% text in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 320, height: 960 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await renderSurfaceState(page, SURFACE_STATES.pairing_waiting);
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    const heading = page.locator('#desktop-entry-title');
    await heading.focus();
    const original = await heading.boundingBox();
    for (const name of ['pairing_failed', 'pairing_compare', 'activation_required', 'setup_complete']) {
      await page.evaluate(state => window.__surfaceFixture.change(state, true), SURFACE_STATES[name]);
      await expect(page.locator('main')).toHaveAttribute('data-requirement', 'desktop-handoff');
      await expect(page.locator('main')).toHaveAttribute('data-task', 'desktop');
      await expect(heading).toBeFocused();
      expect(await heading.boundingBox()).toEqual(original);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
      await expect(page.locator('#open-desktop')).toBeEnabled();
    }
    await page.screenshot({ path: info.outputPath(`handoff-320-200-${theme}.png`), fullPage: true });
  });
}

for (const width of [1280, 390, 320]) {
  for (const theme of ['light', 'dark']) {
    for (const name of ['start_choice', 'software_activation', 'mode_choice', 'preview_complete', 'permission_required',
      'desktop_app_needed', 'extension_connection_unknown', 'full_tab_sleeping', 'full_access_revoked',
      'pairing_required', 'pairing_failed', 'activation_required', 'setup_complete', 'runtime_unavailable']) {
      test(`${name} uses the persistent workspace at ${width}px in ${theme}`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 960 });
        await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
        await renderSurfaceState(page, { ...SURFACE_STATES[name], browserTask: name === 'software_activation' || SURFACE_STATES[name].browserTask });
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
    await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, browserTask: true });
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    const brandHeight = await page.locator('.onboarding-header .product-name').evaluate((element) => element.getBoundingClientRect().height);
    expect(brandHeight).toBeLessThan(130);
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
  const full = page.locator('#start-preview');
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
    await page.locator('#desktop-entry-title').evaluate((element) => {
      element.textContent = 'The connection to the selected creator account and this computer could not be confirmed.';
    });
    await page.locator('#feedback').evaluate((element) => {
      element.textContent = 'The connection could not be checked. Your earlier request may still be processing. Return to the current setup page before starting another attempt.';
    });
    const action = page.locator('#open-desktop');
    await action.scrollIntoViewIfNeeded();
    await expect(action).toBeInViewport();
    const rects = await page.evaluate(() => {
      const rect = id => document.getElementById(id).getBoundingClientRect();
      return { title: rect('desktop-entry-title').toJSON(), action: rect('open-desktop').toJSON(),
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
    await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true, sessionValues: { onboarding_code_entry_v1: '11111111-1111-4111-8111-111111111111' } });
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
    await expect(page.locator('#start-title')).toBeFocused();
  });
}

for (const name of ['software_activation', 'mode_choice', 'permission_required', 'desktop_app_needed',
  'pairing_required', 'pairing_failed', 'activation_required', 'preview_complete', 'runtime_unavailable']) {
  test(`setup-code shortcut is absent from ${name}`, async ({ page }) => {
    await renderSurfaceState(page, { ...SURFACE_STATES[name], receiving: true,
      browserTask: name === 'software_activation' });
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
  await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true, sessionValues: { onboarding_code_entry_v1: '11111111-1111-4111-8111-111111111111' } });
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  await page.evaluate(() => window.__surfaceFixture.change({ agreement: false, mode: 'full', reachable: false }));
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
  await expect(page.locator('#desktop-entry-title')).toBeFocused();
  await expect(page.locator('#desktop-entry-title')).toBeVisible();
});

test('code-entry focus reaches the recovery heading after an intermediate unavailable read', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, receiving: true, sessionValues: { onboarding_code_entry_v1: '11111111-1111-4111-8111-111111111111' } });
  await expect(page.locator('#setup-transfer-code')).toBeFocused();
  await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: true }));
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#setup-transfer-entry')).toBeHidden();
  await expect(page.locator('#setup-transfer-shortcut')).toBeHidden();
  await expect(page.locator('#runtime-title')).toBeFocused();
});

const position = (page) => page.evaluate(() => {
  const rect = (selector) => document.querySelector(selector).getBoundingClientRect().toJSON();
  return { heading: rect('main h2:focus'), height: innerHeight,
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
      await page.locator('#start-preview').click();
      await expect(page.locator('#activation-title')).toBeFocused();
      await page.locator('#terms-accepted').check();
      await page.locator('#risk-acknowledged').check();
      const consent = page.locator('#activate-software');
      await consent.scrollIntoViewIfNeeded();
      await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
      expect(await page.evaluate(() => scrollY)).toBeGreaterThan(0);
      await consent.click();
      await expect(page.locator('#access-title')).toBeFocused();
      const access = await position(page);
      expect(access.heading.top).toBeGreaterThanOrEqual(0);
      expect(access.heading.bottom).toBeLessThanOrEqual(access.height);
      expect(access.overflow, JSON.stringify(access.overflowNodes)).toBe(false);
      await expect(page.locator('#access-card .back-button')).toHaveCount(0);
      await page.evaluate(() => window.__surfaceFixture.change({ permissionGranted: true }));
      await page.locator('#restore-access').click();
      await expect(page.locator('#journey-title')).toBeFocused();
      const connection = await position(page);
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
  await page.locator('#start-preview').click();
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
  await page.locator('#start-preview').click();
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
