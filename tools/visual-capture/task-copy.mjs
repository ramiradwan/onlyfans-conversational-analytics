import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const { tier3: { staticUi } } = JSON.parse(await readFile(new URL('../../frontend/src/theme/tokens.json', import.meta.url), 'utf8'));

/** Checks visible task hierarchy separately from the protected disclosure content. */
export async function inspectTaskCopy(page, fixture) {
  const result = {};
  if (fixture.surface === 'popup') {
    assert.equal(await page.locator('#pre-mode, #companion-pairing, #delete-local-data').count(), 0, 'setup or destructive controls leaked into the popup');
    if (fixture.name === 'preview') {
      assert(await page.locator('#preview-metrics').isVisible(), 'Preview counts are missing');
      assert.equal(await page.locator('#journey-title').innerText(), 'Ready');
    }
    if (fixture.name === 'full_ready') assert.equal(await page.locator('#journey-primary').textContent(), 'Open analysis');
    return result;
  }
  if (fixture.surface === 'setup') {
    if (['software_activation', 'mode_choice', 'mode_choice_full', 'full_review'].includes(fixture.name)) {
      assert(!await page.locator('#journey-card').isVisible(), 'another task competes with required review');
    }
    assert.equal(await page.locator('#preview-metrics, #delete-local-data').count(), 0);
    if (fixture.name === 'preview_complete') assert(!await page.locator('[data-step="connect"]').isVisible());
    if (fixture.name === 'pairing_compare') assert((await page.locator('#pairing-label').innerText()).includes('confirm in the desktop app'));
    return result;
  }
  if (fixture.surface === 'options') {
    assert((await page.locator('#data').innerText()).includes('Messages already stored by the desktop app stay there.'));
    return result;
  }
  const feedback = page.locator('#provisioning-status');
  const message = (await feedback.textContent()).trim();
  assert.equal(await feedback.evaluate((node) => getComputedStyle(node).fontWeight), '400');
  if (message && fixture.name !== 'completed') {
    assert(await feedback.evaluate((node) => node.closest('[aria-current="step"]') !== null), 'feedback is detached from its task');
  }
  result.feedback = message;
  const stage = await page.locator('.provisioning-stage').boundingBox();
  const stageToken = page.viewportSize().width < 600 ? staticUi.provisioningStageBlockNarrow : staticUi.provisioningStageBlock;
  const rootFontSize = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
  assert.equal(stage.height, parseFloat(stageToken) * rootFontSize, 'Stage must preserve the token-backed minimum through each normal step');
  const rail = await page.locator('.progress-rail').boundingBox();
  assert.equal(rail.height, 52);
  assert.equal(await page.locator('textarea, details').count(), 0);
  if (fixture.name.startsWith('approval-unavailable')) {
    assert.equal(message, '', 'routine instructions repeat in a banner');
    assert(await page.locator('#recovery-open').isVisible());
    assert(!await page.locator('#acquire-association').isVisible());
  }
  if (fixture.name === 'confirm') {
    assert.equal(await page.locator('#extension-install-reason').isVisible(), await page.locator('#open-extension-setup').isVisible(), 'Account confirmation must not request completed extension setup');
  }
  return { ...result, stageHeight: stage.height, railHeight: rail.height };
}
