import assert from 'node:assert/strict';

/** Checks visible task hierarchy separately from the protected disclosure content. */
export async function inspectTaskCopy(page, fixture) {
  const result = {};
  if (fixture.surface === 'popup') {
    assert.equal(await page.locator('#pre-mode, #companion-pairing, #delete-local-data').count(), 0, 'setup or destructive controls leaked into the popup');
    if (fixture.name === 'preview') {
      assert(await page.locator('#preview-metrics').isVisible(), 'Preview counts are missing');
      assert(!await page.locator('#journey-title').isVisible(), 'Preview repeats its ready status');
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
  const rows = await page.locator('.step[data-state="completed"]').evaluateAll((nodes) => nodes.map((node) => ({
    shadow: getComputedStyle(node).boxShadow,
    statusX: node.querySelector('.step-state').getBoundingClientRect().right,
    titleRight: node.querySelector('.step-title-group').getBoundingClientRect().right,
  })));
  rows.forEach((row) => {
    assert.equal(row.shadow, 'none', 'completed work still competes with the task card');
    assert(Math.abs(row.statusX - row.titleRight) <= 1, 'step statuses have inconsistent alignment');
  });
  if (fixture.name.startsWith('approval-unavailable')) {
    assert.equal(message, '', 'routine instructions repeat in a banner');
    const card = page.locator('#binding-step');
    const text = await card.innerText();
    result.activeWords = text.trim().split(/\s+/).length;
    assert(result.activeWords <= 65, 'the approval task has accumulated extra prose');
    assert(!/Creator approval|contact support|waiting for completion|;/.test(text));
    assert.equal(await card.locator('.step-description:visible').count(), 1);
    assert.equal(await page.locator('#acquire-association').getAttribute('aria-describedby'), 'creator-approval-unavailable');
    assert(await page.locator('.recovery-help summary').isVisible());
    const button = await page.locator('#acquire-association').boundingBox();
    assert(button && button.y + button.height <= page.viewportSize().height, 'next action falls below the initial viewport');
  }
  return { ...result, completedRows: rows };
}
