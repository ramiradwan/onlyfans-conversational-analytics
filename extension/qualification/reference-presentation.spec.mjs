import { test, expect } from '@playwright/test';
import { SURFACE_FIXTURE_ORIGIN, SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';
import { assertBackgroundTabLayout, backgroundTabLayoutCases, measureBackgroundTabLayout } from './setup-background-tab-layout.mjs';

for (const request of ['ofca.ui.status', 'ofca.legal-activation.status']) {
  test(`initial ${request} delay keeps setup status visible until the Full task is ready`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 960 });
    const rendering = renderSurfaceState(page, { ...SURFACE_STATES.mode_choice_full, initialRequestHold: request });
    await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
    await expect(page.locator('#loading')).toBeVisible();
    await expect(page.locator('#loading')).toHaveAccessibleName('Loading setup');
    await expect(page.locator('#loading')).toHaveText('');
    await expect(page.locator('#loading')).toHaveAttribute('role', 'status');
    const bars = await page.locator('#loading .loading-placeholder > span').evaluateAll((nodes) => {
      const [first] = nodes;
      const previous = getComputedStyle(first).backgroundColor;
      first.style.backgroundColor = 'var(--dipsy-intent-surface-placeholder)';
      const expected = getComputedStyle(first).backgroundColor;
      first.style.removeProperty('background-color');
      return { count: nodes.length, colors: [previous, ...nodes.slice(1).map(node => getComputedStyle(node).backgroundColor)],
        expected, actionHeight: nodes.at(-1).getBoundingClientRect().height,
        controlHeight: Number.parseFloat(getComputedStyle(first).getPropertyValue('--dipsy-static-control-height')) };
    });
    expect(bars.count).toBe(4);
    expect(bars.colors).toEqual(Array(4).fill(bars.expected));
    expect(bars.actionHeight).toBe(bars.controlHeight);
    await expect(page.locator('main')).toBeHidden();
    await expect(page.locator('.onboarding-footer')).toBeHidden();
    const header = await page.locator('.onboarding-header').boundingBox();
    await page.evaluate(() => window.__releaseInitialRequest());
    await rendering;
    await expect(page.locator('main')).toBeVisible();
    await expect(page.locator('#full-disclosure').getByRole('heading', { name: 'Full', exact: true })).toBeVisible();
    await expect(page.locator('#setup-progress')).toHaveCount(0);
    await expect(page.locator('#loading')).toBeHidden();
    expect(await page.locator('.onboarding-header').boundingBox()).toEqual(header);
    await expect(page.locator('main')).toHaveAttribute('data-task', 'consent:full');
    await page.locator('#full-secondary').click();
    await expect(page.locator('#preview-disclosure').getByRole('heading', { name: 'Preview', exact: true })).toBeVisible();
    await expect(page.locator('#setup-progress')).toBeHidden();
    await expect(page.locator('main')).toHaveAttribute('data-task', 'consent:preview');
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

for (const theme of ['light', 'dark']) {
  for (const [width, scale] of [[1440, 1], [390, 1], [390, 2], [320, 1], [320, 2]]) {
    test(`exhausted runtime points to desktop help at ${width}px, ${scale * 100}% text, ${theme}`, async ({ page, context }, info) => {
      await page.emulateMedia({ colorScheme: theme });
      await page.setViewportSize({ width, height: 960 });
      await page.addInitScript(size => {
        document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.fontSize = size + 'px'; });
        window.__supportClipboardWrites = 0;
        Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => {
          window.__supportClipboardWrites++;
          throw new DOMException('Clipboard unavailable', 'NotAllowedError');
        } } });
      }, 16 * scale);
      const supportChannels = { telegram: 'https://chat.example.com/help-and-setup', tiktok: 'https://video.example.com/a&copy;b',
        whatsapp: 'https://help.example.com/<script>', extra: 'https://example.com/' };
      const outbound = [], newPages = [];
      page.on('request', request => { if (new URL(request.url()).origin !== SURFACE_FIXTURE_ORIGIN) outbound.push(request.url()); });
      context.on('page', opened => { if (opened !== page) newPages.push(opened); });
      await page.clock.install();
      await renderSurfaceState(page, { ...SURFACE_STATES.runtime_unavailable, portUnavailable: true, supportChannels });
      const card = page.locator('#runtime-unavailable');
      const title = card.locator('h2').first(), body = card.locator('#runtime-body'), footer = page.locator('.onboarding-footer');
      await expect(body).toHaveText('Connection interrupted. Reconnecting…');
      await expect(card.locator('[data-support-help]')).toHaveCount(0);
      const titleBefore = await title.boundingBox(), footerBefore = await footer.boundingBox();
      await page.clock.runFor(1800);
      await expect(body).toHaveText('Open the desktop app for help.');
      await expect(card.locator('[data-support-help], a, button, input')).toHaveCount(0);
      await expect(card).not.toContainText(/https?:\/\//);
      expect(await title.boundingBox()).toEqual(titleBefore);
      const main = await page.locator('main').boundingBox();
      expect(await footer.boundingBox()).toEqual({
        ...footerBefore, y: Math.max(960 - footerBefore.height, main.y + main.height),
      });
      if (width === 1440) {
        const lines = await card.evaluate(element => [...element.querySelectorAll('#runtime-title, #runtime-body')].map(node => {
          const range = document.createRange();
          range.selectNodeContents(node);
          return range.getClientRects().length;
        }));
        expect(lines).toEqual([1, 1]);
      }
      expect(await footer.getByText('Privacy information unavailable')).toBeVisible();
      const geometry = await body.evaluate(element => ({
        textWidth: element.scrollWidth, bodyWidth: element.clientWidth,
        viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth,
      }));
      expect(geometry.content).toBe(geometry.viewport);
      expect(geometry.textWidth).toBe(geometry.bodyWidth);
      await body.scrollIntoViewIfNeeded();
      await expect(body).toBeInViewport();
      await footer.scrollIntoViewIfNeeded();
      await expect(footer).toBeInViewport();
      const safeReads = await page.evaluate(() => window.__surfaceFixture.calls.every(({ type, action }) =>
        type === 'ofca.ui.status' || type === 'ofca.legal-activation.status'
        || type === 'ofca.workspace.v1' && ['read', 'browser_task', 'desktop_adoption'].includes(action)));
      expect(safeReads).toBe(true);
      expect(await page.evaluate(() => window.__supportClipboardWrites)).toBe(0);
      expect(outbound).toEqual([]);
      expect(newPages).toEqual([]);
      await page.screenshot({ path: info.outputPath('terminal-help.png'), fullPage: true });
    });
  }
}

for (const [state, action] of [
  [{ ...SURFACE_STATES.runtime_unavailable, portUnavailable: true }, '#runtime-title'],
  [{ ...SURFACE_STATES.legal_unavailable, portUnavailable: true, supportChannels: { telegram: 'https://chat.example.com/help' } }, '#legal-unavailable'],
  [{ ...SURFACE_STATES.desktop_app_needed, portUnavailable: true, supportChannels: { telegram: 'https://chat.example.com/help' } }, '#open-desktop'],
]) {
  test(`support stays omitted with an empty projection or the ${action} remedy`, async ({ page }) => {
    await page.clock.install();
    await renderSurfaceState(page, state);
    await page.clock.runFor(1800);
    await expect(page.locator(action)).toBeVisible();
    await expect(page.locator('[data-support-help]')).toHaveCount(0);
    if (action === '#runtime-title') await expect(page.locator('#runtime-body')).toHaveText('Open the desktop app for help.');
    if (action === '#legal-unavailable') await expect(page.getByText('Update the extension to continue.')).toBeVisible();
  });
}

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
  await expect(page.locator('main')).toHaveAttribute('data-task', 'consent:full');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'consent:initial:full');
  await expect(page.locator('#review-notice')).toBeHidden();
  const disclosure = await page.locator('#start-disclosure').textContent();
  const copy = page.locator('#full-disclosure .points p, #separate-choices-disclosure p');
  const measureCopy = () => copy.evaluateAll((paragraphs) => paragraphs.map((paragraph) => ({
    font: getComputedStyle(paragraph).fontSize, height: paragraph.getBoundingClientRect().height,
  })));
  const sizes = await measureCopy();
  expect(sizes.every(({ height }) => height > 0)).toBe(true);
  await page.locator('#terms-accepted').focus();
  await page.evaluate(() => window.__surfaceFixture.change({ mode: 'paused', resume: 'full', reauthorization: true }));
  await expect(page.locator('#review-notice')).toBeVisible();
  await expect(page.locator('main')).toHaveAttribute('data-task', 'consent:full');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'consent:reauthorization:full');
  await expect(page.locator('#terms-accepted')).toBeFocused();
  expect(await page.locator('#start-disclosure').textContent()).toBe(disclosure);
  expect(await measureCopy()).toEqual(sizes);
});

for (const theme of ['light', 'dark']) {
  test(`Full handoff keeps its focus and heading anchor at 320px and 200% text in ${theme}`, async ({ page }, info) => {
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
      const { height, ...anchor } = await heading.boundingBox();
      expect(anchor).toEqual({ x: original.x, y: original.y, width: original.width });
      const textHeight = await heading.evaluate((element) => {
        const range = document.createRange();
        range.selectNodeContents(element);
        return range.getBoundingClientRect().height;
      });
      expect(height).toBeGreaterThanOrEqual(textHeight);
      expect(height - textHeight).toBeLessThan(await heading.evaluate((element) => parseFloat(getComputedStyle(element).lineHeight)));
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
      if (name === 'pairing_compare') await expect(page.locator('#desktop-comparison-code')).toBeVisible();
      else await expect(page.locator('#desktop-comparison-code')).toBeHidden();
      if (name === 'pairing_compare') await expect(page.locator('#desktop-comparison-code')).toHaveCSS('font-size', '80px');
      await expect(page.locator('#open-desktop')).toBeEnabled();
    }
    await page.screenshot({ path: info.outputPath(`handoff-320-200-${theme}.png`), fullPage: true });
  });
}

for (const [width, textSize] of [[1440, 16], [390, 16], [320, 32]]) {
  for (const theme of ['light', 'dark']) {
    test(`visual review: desktop launch keeps the body and actions anchored at ${width}px in ${theme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 960 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
      await renderSurfaceState(page, SURFACE_STATES.full_handoff);
      await page.evaluate((size) => {
        document.documentElement.style.fontSize = `${size}px`;
        const send = chrome.runtime.sendMessage;
        chrome.runtime.sendMessage = (message) => message.action !== 'prepare_launch' ? send(message)
          : new Promise(resolve => { window.__finishLaunch = () => resolve({ ok: false }); });
      }, textSize);
      const layout = () => page.locator('#desktop-entry').evaluate(card => {
        const top = card.getBoundingClientRect().top;
        return [...card.querySelectorAll('#desktop-entry-title, #desktop-entry-body, #desktop-setup-code, button')].map(node => {
          const rect = node.getBoundingClientRect();
          return [rect.top - top, rect.height];
        });
      });
      const body = page.locator('#desktop-entry-body');
      const originalBody = await body.textContent();
      const original = await layout();
      const action = page.locator('#open-desktop');
      const spot = page.locator('#desktop-icon-mark');
      const status = page.locator('#desktop-launch-status');
      await action.click();
      await expect(status).toHaveText('Waiting for the desktop app…');
      await expect(spot).toHaveAttribute('data-icon', 'app');
      await expect(status.locator('#desktop-launch-mark')).toHaveAttribute('data-icon', 'pending');
      await expect(action).toBeDisabled();
      expect(await action.evaluate((button) => {
        const context = document.createElement('canvas').getContext('2d');
        const style = getComputedStyle(button);
        const lightness = (color) => {
          context.fillStyle = color;
          context.fillRect(0, 0, 1, 1);
          const rgb = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).map(value => value / 255);
          return rgb.reduce((sum, value, index) => sum + [0.2126, 0.7152, 0.0722][index]
            * (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4), 0);
        };
        const [dark, light] = [lightness(style.color), lightness(style.backgroundColor)].sort((left, right) => left - right);
        return (light + 0.05) / (dark + 0.05);
      })).toBeGreaterThanOrEqual(4.5);
      await expect(body).toHaveText(originalBody);
      expect(await layout()).toEqual(original);
      await page.evaluate(() => window.__finishLaunch());
      await expect(status).toHaveText('Desktop connection not confirmed');
      await expect(action).toBeEnabled();
      expect(await layout()).toEqual(original);
      await page.evaluate(() => window.__surfaceFixture.change({ mode: 'full', paired: true, reachable: false }));
      await expect(page.locator('#desktop-entry-title')).toHaveText('Desktop connection not confirmed');
      await expect(status).toBeHidden();
      await page.evaluate(() => window.__surfaceFixture.change({ mode: 'preview', paired: false }));
      await page.evaluate(() => window.__surfaceFixture.change({ adopted: true, browserTask: false }));
      await expect(status).toHaveText('The desktop app has this browser’s setup request. Continue there.');
      await expect(spot).toHaveAttribute('data-icon', 'app');
      await expect(status.locator('#desktop-launch-mark')).toHaveAttribute('data-icon', 'done');
      expect(await layout()).toEqual(original);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    });
  }
}

test('visual review: connection status unavailable has no desktop action or setup-code hint', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.extension_connection_unknown);
  await expect(page.locator('#desktop-entry-title')).toHaveText('Connection status unavailable');
  await expect(page.locator('#desktop-entry button:visible')).toHaveCount(0);
  await expect(page.locator('#desktop-setup-code')).toBeHidden();
  await expect(page.locator('#desktop-icon-mark')).toHaveAttribute('data-icon', 'offline');
});

test('visual review: setup readiness uses the approved spot icons and desktop guidance', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_required);
  const body = page.locator('#desktop-entry-body');
  const icon = page.locator('#desktop-icon-mark');
  await expect(body).toHaveText('Continue in the desktop app.');
  await expect(icon).toHaveAttribute('data-icon', 'insights');
  await page.evaluate(() => window.__surfaceFixture.change({ commercial: 'unavailable' }));
  await expect(icon).toHaveAttribute('data-icon', 'offline');
  await page.evaluate(() => window.__surfaceFixture.change({ commercial: 'active', ready: true }));
  await expect(icon).toHaveAttribute('data-icon', 'insights');
  await page.evaluate(() => window.__surfaceFixture.change({ sleeping: true }));
  await expect(icon).toHaveAttribute('data-icon', 'pause');
});

for (const reauthorization of [false, true]) {
  test(`visual review: paused Preview has one status heading with review ${reauthorization}`, async ({ page }) => {
    await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, mode: 'paused', resume: 'preview', reauthorization });
    await expect(page.locator('#journey-title')).toHaveText('Analytics paused');
    await expect(page.locator('#journey-badge')).toBeHidden();
  });
}

for (const width of [1440, 390, 320]) {
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
        if (name === 'permission_required') {
          await expect(page.locator('#access-card .onboarding-icon')).toHaveAttribute('data-icon', 'attention');
        }
        if (width === 1440) {
          await page.evaluate(() => document.fonts.ready);
          const copy = [[1, 'main section > h2:visible'],
            [2, '#desktop-entry-body:visible, #journey-body:visible, #access-body:visible, #runtime-body:visible'],
            [1, '#journey-badge:visible, #desktop-launch-message:visible']];
          for (const [budget, selector] of copy) {
            const lines = await page.locator(selector).evaluateAll((nodes) => nodes.map((node) => {
              const range = document.createRange();
              range.selectNodeContents(node);
              return new Set([...range.getClientRects()].map(rect => rect.y)).size;
            }));
            expect(lines.every(line => line <= budget), selector).toBe(true);
          }
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
    await expect(page.locator('#start-disclosure')).toContainText('no general automatic expiry');
    await page.locator('#terms-accepted').check();
    await page.locator('#risk-acknowledged').check();
    const action = page.locator('#enable-full');
    await expect(action).toBeEnabled();
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
  const action = page.locator('#enable-preview');
  await expect(action).toBeEnabled();
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
  await expect(page.locator('#journey-icon .onboarding-icon')).toHaveAttribute('data-icon', 'pending');
  await expect(page.locator('#journey-title')).toHaveText('Waiting for messages');
});

test('D1 setup shows the admitted state through the persistent entry view', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.setup_complete);
  await expect(page.locator('#desktop-entry-title')).toHaveText('Full analytics is on');
  await expect(page.locator('#desktop-entry-body')).toHaveText('Insights appear in the desktop app.');
  await expect(page.locator('#open-desktop')).toHaveText('Open desktop app');
  await expect(page.locator('#journey-badge')).toBeHidden();
});

for (const [width, scale, theme] of backgroundTabLayoutCases) {
  test(`D2 preserves Preview controls and metrics at ${width}px with ${scale}px text in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 960 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, observer: { attachment: 'ready', helper: 'open' } });
    await page.evaluate((size) => { document.documentElement.style.fontSize = `${size}px`; }, scale);
    const note = page.locator('#background-tab-note');
    const reopen = page.locator('#reopen-background-tab');
    const badge = page.locator('#journey-badge');
    const action = page.locator('#journey-primary');
    const privacy = page.locator('.onboarding-footer #privacy-link');
    const box = (id) => page.locator(id).evaluate((element) => {
      const card = element.closest('#journey-card').getBoundingClientRect();
      const { x, y, width, height } = element.getBoundingClientRect();
      return { x: x - card.x, y: y - card.y, width, height };
    });
    await page.evaluate(() => document.fonts.ready);
    await action.focus();
    if (width === 320) {
      const countLines = await page.locator('#preview-metrics dd').evaluateAll((counts) => counts.map((count) => {
        const range = document.createRange();
        range.selectNodeContents(count);
        return range.getClientRects().length;
      }));
      expect(countLines).toEqual([1, 1, 1]);
    }
    await expect(note).toHaveText('Updates stop if you close the tab the extension opened.');
    await expect(badge).toHaveText('Counting messages');
    await expect(action).toHaveCSS('text-align', 'start');
    expect((await box('#journey-primary')).x).toBe((await box('#pause')).x);
    const iconOffset = await note.evaluate((row) => {
      const [icon, copy] = row.children;
      const iconBox = icon.getBoundingClientRect();
      return iconBox.top + iconBox.height / 2 - copy.getBoundingClientRect().top;
    });
    expect(iconOffset).toBe(Number.parseFloat(await note.evaluate((row) => getComputedStyle(row).lineHeight)) / 2);
    await expect(reopen).toBeHidden();
    const open = await box('#background-tab-note');
    expect((await box('.background-tab-row')).height).toBe(open.height);
    const openLayout = await measureBackgroundTabLayout(page);
    const openScreenshot = info.outputPath(`preview-counting-${theme}.png`);
    await page.screenshot({ path: openScreenshot, fullPage: true });
    await page.keyboard.press('Tab');
    await expect(privacy).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(action).toBeFocused();
    if (width === 320) {
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    const before = [await box('#journey-primary'), await box('#preview-metrics')];
    const badgeBefore = await box('#journey-badge');
    await page.evaluate(() => window.__surfaceFixture.change({ observer: { attachment: 'checking', helper: 'closed' } }));
    await expect(reopen).toHaveText('Reopen creator site tab');
    await expect(reopen).toBeVisible();
    await expect(note).toBeHidden();
    await expect(badge).toHaveText('Updates stopped');
    await expect(action).toBeFocused();
    const badgeAfter = await box('#journey-badge');
    const delta = badgeAfter.height - badgeBefore.height;
    if (width > 390 || scale === 16) expect(delta).toBe(0);
    expect([await box('#journey-primary'), await box('#preview-metrics')]).toEqual(before.map(({ y, ...rect }) => ({ ...rect, y: y + delta })));
    const closed = await box('#reopen-background-tab');
    expect((await box('.background-tab-row')).height).toBe(closed.height);
    expect(closed.y).toBe(open.y + delta);
    if (width === 320) {
      expect(open.height).toBeGreaterThan(closed.height);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    const closedLayout = await measureBackgroundTabLayout(page);
    const closedScreenshot = info.outputPath(`preview-stopped-${theme}.png`);
    await page.screenshot({ path: closedScreenshot, fullPage: true });
    await info.attach('D2 row geometry', { body: JSON.stringify({ width, scale, theme, open: openLayout, closed: closedLayout,
      screenshots: [openScreenshot, closedScreenshot] }), contentType: 'application/json' });
    const reopenCalls = () => page.evaluate(() => window.__surfaceFixture.calls.filter(({ type }) => type === 'ofca.ui.reopen-background-tab').length);
    expect(await reopenCalls()).toBe(0);
    await page.keyboard.press('Tab');
    await expect(reopen).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(privacy).toBeFocused();
    expect(await reopenCalls()).toBe(0);
    await reopen.click();
    await expect.poll(reopenCalls).toBe(1);
    await page.evaluate(() => window.__surfaceFixture.change({ observer: { attachment: 'ready', helper: 'closed' } }));
    await expect(reopen).toBeHidden();
    await expect(note).toBeHidden();
    await expect(badge).toHaveText('Counting messages');
    expect(await reopenCalls()).toBe(1);
    assertBackgroundTabLayout(openLayout, closedLayout, width, scale);
  });
}

test('readiness stays terminal without an owner signal and can recover on a surface change', async ({ page }) => {
  await page.clock.install();
  await renderSurfaceState(page, SURFACE_STATES.activation_unavailable);
  const title = page.locator('#desktop-entry-title');
  await expect(title).toHaveText('Can’t check Full analytics right now');
  await expect(page.getByRole('button', { name: 'Check again', exact: true })).toHaveCount(0);
  await expect(page.locator('#desktop-entry')).not.toContainText('updates by itself');
  await title.focus();
  await page.evaluate(() => Object.assign(window.__surfaceFixture.state, { commercial: 'active', ready: true }));
  await page.clock.runFor(13_000);
  await expect(title).toHaveText('Can’t check Full analytics right now');
  await page.evaluate(() => window.__surfaceFixture.change({}));
  await expect(title).toHaveText('Full analytics is on');
  await expect(title).toBeFocused();
});

test('the owned comparison displays the admitted code until the original attempt ends', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.pairing_compare);
  await expect(page.locator('#desktop-entry-title')).toHaveText('Confirm the connection');
  await expect(page.locator('#desktop-entry-body')).toHaveText('Check that the desktop app shows the same code, then confirm there. Keep this tab open.');
  const code = page.locator('#desktop-comparison-code');
  await expect(code).toHaveText('483 217');
  await expect(code).toHaveCSS('font-size', '44px');
  await expect(code).toHaveCSS('font-weight', '700');
  const bodyBounds = await page.locator('#desktop-entry-body').boundingBox();
  const codeBounds = await code.boundingBox();
  expect(codeBounds.y).toBeGreaterThanOrEqual(bodyBounds.y + bodyBounds.height);
  expect(codeBounds.y + codeBounds.height).toBeLessThanOrEqual((await page.locator('#open-desktop').boundingBox()).y);
  await page.evaluate(() => window.__surfaceFixture.change({ pairing: 'pairing_failed' }));
  await expect(code).toBeHidden();
  await expect(page.locator('#desktop-entry-title')).toHaveText('Connection didn’t finish');
  await expect(page.locator('#desktop-entry-body')).toHaveText('Choose Connect extension in the desktop app to start a new connection.');
  await expect(page.locator('#open-desktop')).toHaveText('Open desktop app');
  await expect(page.getByRole('button', { name: /try connection|check again/i })).toHaveCount(0);
});

test('the onboarding tab offers no setup-code entry and points to the desktop app', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.desktop_app_needed, receiving: true });
  await expect(page.locator('#setup-transfer-entry')).toHaveCount(0);
  await expect(page.locator('#setup-transfer-code')).toHaveCount(0);
  await expect(page.locator('#desktop-setup-code')).toHaveText('Have a setup code? Enter it in the desktop app.');
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
      await renderSurfaceState(page, { ...SURFACE_STATES.start_choice, permissionGranted: false, permissionOutcome: 'granted', reachable: false });
      if (textScale > 1) await page.evaluate((scale) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * scale}px`;
      }, textScale);
      await page.locator('#start-preview').click();
      await expect(page.locator('#activation-title')).toBeFocused();
      await page.locator('#terms-accepted').check();
      await page.locator('#risk-acknowledged').check();
      const consent = page.locator('#enable-preview');
      await expect(consent).toBeEnabled();
      await consent.scrollIntoViewIfNeeded();
      const note = await page.locator('#mode-permission-note').boundingBox();
      const button = await consent.boundingBox();
      expect(note.y + note.height).toBeLessThanOrEqual(button.y);
      await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
      expect(await page.evaluate(() => scrollY)).toBeGreaterThan(0);
      await consent.click();
      await expect(page.locator('#journey-title')).toBeFocused();
      const connection = await position(page);
      expect(connection.heading.top).toBeGreaterThanOrEqual(0);
      expect(connection.heading.bottom).toBeLessThanOrEqual(connection.height);
      expect(connection.overflow, JSON.stringify(connection.overflowNodes)).toBe(false);
      await page.screenshot({ path: info.outputPath(`task-transition-${width}-${textScale}-${theme}.png`), fullPage: true });

      await page.evaluate(() => window.__surfaceFixture.change({ phase: 'permission_required', permissionGranted: false }));
      const accessTitle = page.locator('#access-title');
      await expect(accessTitle).toBeVisible();
      await accessTitle.focus();
      await expect(accessTitle).toBeFocused();
      await expect(page.locator('#access-card .onboarding-icon')).toHaveAttribute('data-icon', 'attention');
      const access = await position(page);
      expect(access.heading.top).toBeGreaterThanOrEqual(0);
      expect(access.heading.bottom).toBeLessThanOrEqual(access.height);
      expect(access.overflow, JSON.stringify(access.overflowNodes)).toBe(false);
      await expect(page.locator('#access-card .back-button')).toHaveCount(0);
      await page.locator('#restore-access').click();
      await expect(page.locator('#journey-title')).toBeFocused();
      await expect(page.locator('#preview-metrics')).toBeVisible();
    });
  }
}

test('validation focus and passive updates keep the current reading position', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await renderSurfaceState(page, SURFACE_STATES.start_choice);
  await page.locator('#start-preview').click();
  await expect(page.locator('#enable-preview')).toBeDisabled();
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#enable-preview')).toBeDisabled();
  await page.locator('#risk-acknowledged').focus();
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
