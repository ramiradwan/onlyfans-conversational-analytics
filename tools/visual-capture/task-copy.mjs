import assert from 'node:assert/strict';

/** Checks visible task hierarchy separately from the protected disclosure content. */
export async function inspectTaskCopy(page, fixture) {
  const result = {};
  if (fixture.surface === 'popup') {
    if (['full_ready', 'preview', 'paused'].includes(fixture.name)) {
      assert(!await page.locator('#mode-label').isVisible(), 'home repeats status in its header');
      assert(!await page.locator('#journey-badge').isVisible(), 'home repeats status in a badge');
    }
    if (fixture.name === 'full_ready') {
      assert(!await page.locator('#journey-body').isVisible(), 'ready state repeats its heading');
      assert.equal(await page.locator('#journey-primary').textContent(), 'Open analysis');
      result.readyText = await page.locator('#journey-card').innerText();
    }
    if (['software_activation', 'mode_choice', 'mode_choice_full', 'full_review'].includes(fixture.name)) {
      assert(!await page.locator('#journey-card').isVisible(), 'an extra task competes with the required review');
      assert(!await page.locator('#preview-metrics').isVisible(), 'metrics compete with required review');
    }
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
