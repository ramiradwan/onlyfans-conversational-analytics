import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright';
import { installProvisioningFixture, openProvisioningFixture } from './provisioning-driver.mjs';

for (const width of [390, 1440]) for (const outcome of ['pending', 'refused']) {
  test(`finalization description follows ${outcome} state at ${width}`, async () => {
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await installProvisioningFixture(page, { stage: 'finalization_ready', name: 'finish' });
      await openProvisioningFixture(page, { pendingOperation: 'finalize' });
      await page.waitForFunction(() => window.__provisioningFixture.calls.includes('finalize'));
      if (outcome === 'refused') {
        await page.evaluate(() => {
          window.__provisioningFixture.refusal = 'binding_acquisition_unavailable';
          window.__provisioningFixture.release('finalize');
        });
        await page.locator('#finalize-provisioning').waitFor({ state: 'visible' });
      }
      const description = page.locator('#finalize-step-description');
      assert(await description.isVisible());
      assert.equal(await page.locator('.provisioning-stage').getAttribute('data-complete'), 'false');
      assert.equal(await description.innerText(), outcome === 'pending'
        ? 'Finishing setup…' : 'Setup did not finish. Try again.');
      if (outcome === 'pending') {
        await page.evaluate(() => window.__provisioningFixture.release('finalize'));
        await page.getByRole('heading', { name: 'Setup finished' }).waitFor();
        assert.equal(await description.innerText(), 'Restarting the desktop app…');
      }
    } finally { await browser.close(); }
  });
}
