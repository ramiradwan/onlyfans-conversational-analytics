import assert from 'node:assert/strict';

export async function assertSetupTaskGeometry(page) {
  const result = await page.evaluate(() => {
    const main = document.querySelector('main');
    const visible = node => node?.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
    if (!visible(main)) return { unresolved: true, recovery: visible(document.querySelector('#runtime-unavailable')) };
    const headings = [...main.querySelectorAll('h2')].filter(visible);
    const documentHeight = document.scrollingElement.scrollHeight;
    const accessible = node => {
      const box = node.getBoundingClientRect();
      const left = box.left + scrollX, top = box.top + scrollY;
      return box.width > 0 && box.height > 0 && left >= 0 && left + box.width <= innerWidth + 1
        && top >= 0 && top + box.height <= documentHeight + 1;
    };
    return { task: main.dataset.task, requirement: main.dataset.requirement, unresolved: false, headings: headings.length,
      headingsReachable: headings.every(accessible),
      controlsReachable: [...main.querySelectorAll('button, a[href], input')].filter(visible).every(accessible),
      horizontalOverflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > innerWidth + 1,
      railVisible: visible(document.querySelector('#setup-progress')),
      fullProgress: document.querySelector('.onboarding-layout').dataset.progress === 'full' };
  });
  if (result.unresolved) {
    assert(result.recovery, 'A setup task must render or show its supported unavailable state');
    return result;
  }
  assert(result.task, 'Setup must identify its confirmed task');
  assert(result.requirement, 'Setup must identify its confirmed customer requirement');
  assert(result.headings > 0 && result.headingsReachable, 'Task headings must be reachable through page scrolling');
  assert(result.controlsReachable && !result.horizontalOverflow, 'Task actions must remain reachable without horizontal scrolling');
  assert.equal(Boolean(result.railVisible), result.fullProgress, 'The progress rail must follow the confirmed Full setup task');
  return result;
}
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
    result.geometry = await assertSetupTaskGeometry(page);
    if (['software_activation', 'mode_choice', 'mode_choice_full', 'full_review'].includes(fixture.name)) {
      assert(!await page.locator('#journey-card').isVisible(), 'another task competes with required review');
    }
    assert.equal(await page.locator('#delete-local-data').count(), 0, 'Destructive controls must remain in Options');
    assert.equal(await page.locator('#preview-metrics').isVisible(),
      ['preview_complete', 'reload_required'].includes(fixture.name),
      'Preview counts belong in the persistent ready workspace, outside required review');
    if (fixture.name === 'preview_complete') assert(!await page.locator('[data-step="connect"]').isVisible());
    const fullProgress = await page.locator('.onboarding-layout').getAttribute('data-progress') === 'full';
    assert.equal(await page.locator('#setup-progress').isVisible(), fullProgress, 'The progress rail must follow the confirmed Full setup task');
    if (fullProgress) assert.equal(await page.locator('#setup-progress [aria-current="step"]').count(), 1, 'Full setup must identify its current task');
    if (fixture.name === 'pairing_compare') assert((await page.locator('#pairing-label').innerText()).includes('confirm in the desktop app'));
    return result;
  }
  if (fixture.surface === 'options') {
    assert((await page.locator('#data').innerText()).includes('Messages already stored by the desktop app stay there.'));
    return result;
  }
  throw new Error('Unknown static task surface: ' + fixture.surface);
}
