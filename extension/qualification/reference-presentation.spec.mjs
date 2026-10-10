import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';

for (const width of [1280, 390, 320]) {
  for (const theme of ['light', 'dark']) {
    for (const name of ['start_choice', 'software_activation', 'mode_choice', 'preview_complete', 'permission_required',
      'desktop_app_needed', 'pairing_required', 'pairing_failed', 'activation_required', 'setup_complete', 'runtime_unavailable']) {
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

test('Preview waiting does not display a verified icon before observer attachment', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, observer: { attachment: 'checking', helper: 'none' } });
  await expect(page.locator('#journey-icon')).not.toHaveText('✓');
});

for (const width of [1280, 390, 320]) for (const theme of ['light', 'dark']) {
  test(`receiving code validation stays in its workspace at ${width}px in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 960 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, receiving: true });
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
