import { test, expect } from '@playwright/test';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { POPUP_STATES as STATES, renderPopupState as renderState } from './popup-visual-fixtures.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('visual state copy matches the popup sources', async () => {
  const sources = (await Promise.all(
    ['popup.html', 'popup.js', 'runtime/customer-journey.mjs'].map((file) => readFile(path.join(ROOT, file), 'utf8')),
  )).join('\n');
  for (const state of Object.values(STATES)) {
    for (const key of ['mode', 'badge', 'title', 'body', 'primary', 'secondary', 'activation', 'analysis']) {
      if (state[key] === undefined) continue;
      const literals = [`'${state[key]}'`, `>${state[key]}<`];
      expect(literals.some((literal) => sources.includes(literal)), `${key}: ${state[key]}`).toBe(true);
    }
  }
});

for (const [name, state] of Object.entries(STATES)) {
  test(`visual state: ${name}`, async ({ page }, testInfo) => {
    await renderState(page, state);
    await expect(page.locator('#journey-title')).toHaveText(state.title);
    await expect(page.locator('#journey-card')).toHaveAttribute('data-tone', state.tone);
    if (state.desktop !== undefined) {
      await expect(page.locator('#activation-status')).toHaveText(state.activation);
      await expect(page.locator('#analysis-status')).toHaveText(state.analysis);
    }
    const output = process.env.OFCA_UX_SCREENSHOT_DIR
      ? path.join(process.env.OFCA_UX_SCREENSHOT_DIR, `extension-${name}.png`)
      : testInfo.outputPath(`extension-${name}.png`);
    await mkdir(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output, fullPage: true });
    await testInfo.attach(`extension-${name}`, { path: output, contentType: 'image/png' });
  });
}
