import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';

for (const [name, state] of Object.entries(SURFACE_STATES)) {
  for (const scheme of ['light', 'dark']) {
    for (const width of state.surface === 'popup' ? [390, 320] : [1440, 390]) {
      test(`visual state: ${name} ${scheme} ${width}`, async ({ page }, testInfo) => {
        const errors = [];
        page.on('pageerror', (error) => errors.push(error.message));
        await page.setViewportSize({ width, height: state.surface === 'popup' ? 600 : 900 });
        await page.emulateMedia({ colorScheme: scheme });
        await renderSurfaceState(page, state);
        await expect(page.locator('h1')).toBeVisible();
        expect(await page.evaluate(() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) - innerWidth)).toBeLessThanOrEqual(1);
        if (state.surface === 'popup') {
          await expect(page.locator('#pre-mode, #mode-choice, #companion-pairing, #delete-local-data')).toHaveCount(0);
          if (state.mode === 'preview') await expect(page.locator('#preview-metrics')).toBeVisible();
        }
        if (state.pairing === 'compare' && state.surface === 'setup') await expect(page.locator('#pairing-code')).toHaveText('483 217');
        expect(errors).toEqual([]);
        const path = testInfo.outputPath(`${state.surface}-${name}-${scheme}-${width}.png`);
        await page.screenshot({ path, fullPage: true });
        await testInfo.attach('surface', { path, contentType: 'image/png' });
      });
    }
  }
}
