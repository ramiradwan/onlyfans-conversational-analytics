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
    if (fixture.name === 'full_ready') assert.equal(await page.locator('#journey-primary').textContent(), 'Continue in the desktop app');
    return result;
  }
  if (fixture.surface === 'setup') {
    result.geometry = await assertSetupTaskGeometry(page);
    if (['mode_choice', 'mode_choice_full', 'full_review'].includes(fixture.name)) {
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
    if (['software_activation', 'software_activation_ready', 'full_handoff', 'pairing_compare'].includes(fixture.name)) {
      assert(await page.locator('#desktop-entry').isVisible(), 'Full setup and pairing decisions belong in the desktop app');
      assert(await page.locator('#open-desktop').isEnabled(), 'The admitted workspace must offer desktop return');
      assert.equal(await page.locator('#companion-pairing').count(), 0, 'The retired extension pairing wizard must not return');
    }
    return result;
  }
  if (fixture.surface === 'options') {
    assert((await page.locator('#data').innerText()).includes('Messages already stored by the desktop app stay there.'));
    return result;
  }
  if (fixture.surface === 'desktop-onboarding' || fixture.surface === 'browser-task') {
    const summary = await page.evaluate((surface) => {
      const main = document.querySelector('main');
      const card = document.querySelector('.setup-card');
      const title = document.querySelector('#task-title');
      const visible = node => node && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden' && getComputedStyle(node).display !== 'none';
      const documentHeight = document.scrollingElement.scrollHeight;
      const withinPage = node => {
        const box = node.getBoundingClientRect();
        const left = box.left + scrollX, top = box.top + scrollY;
        return box.width > 0 && box.height > 0 && left >= -1 && left + box.width <= innerWidth + 1 && top >= 0 && top + box.height <= documentHeight + 1;
      };
      const controls = [...main.querySelectorAll('button, a[href]')].filter(visible);
      const overlappingControls = [];
      for (let i = 0; i < controls.length; i++) for (const other of controls.slice(i + 1)) {
        const a = controls[i].getBoundingClientRect(), b = other.getBoundingClientRect();
        if (Math.min(a.right, b.right) > Math.max(a.left, b.left) + 0.5 && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top) + 0.5) {
          overlappingControls.push([controls[i].id || controls[i].textContent.trim(), other.id || other.textContent.trim()]);
        }
      }
      const clippedText = [];
      const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode;
        if (!text.textContent.trim() || !visible(text.parentElement)) continue;
        const range = document.createRange();
        range.selectNodeContents(text);
        for (const box of range.getClientRects()) {
          if (box.left < -1 || box.right > innerWidth + 1) clippedText.push(text.textContent.trim().slice(0, 40));
          for (let ancestor = text.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
            const css = getComputedStyle(ancestor);
            const bounds = ancestor.getBoundingClientRect();
            if ((/(hidden|clip)/u.test(css.overflowX) && (box.left < bounds.left - 1 || box.right > bounds.right + 1))
              || (/(hidden|clip)/u.test(css.overflowY) && (box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1))) {
              clippedText.push(text.textContent.trim().slice(0, 40));
            }
          }
        }
      }
      const brokenImages = [...document.querySelectorAll('img')].filter(img => visible(img) && (!img.complete || img.naturalWidth === 0)).length;
      return {
        journeyState: main?.dataset.journeyState ?? null,
        titleText: title?.textContent?.trim() ?? '',
        titleVisible: visible(title) && withinPage(title),
        statusText: document.querySelector('#task-status')?.textContent?.trim() ?? '',
        controlsReachable: controls.every(withinPage),
        overlappingControls,
        clippedText,
        brokenImages,
        horizontalOverflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > innerWidth + 1,
        steps: document.querySelectorAll('[data-step]').length,
        hasNav: Boolean(document.querySelector('nav')),
        hasRecoveryInput: Boolean(document.querySelector('#recovery-package, textarea')),
        surface,
      };
    }, fixture.surface);
    assert(summary.titleVisible && summary.titleText.length > 0, 'Task title must be visible and readable');
    assert(summary.controlsReachable, 'Visible controls must fit within the page bounds');
    assert.deepEqual(summary.overlappingControls, [], 'Visible controls must not overlap');
    assert.deepEqual(summary.clippedText, [], 'Readable task copy must not be clipped');
    assert.equal(summary.brokenImages, 0, 'Provider images must load cleanly');
    assert(!summary.horizontalOverflow, 'Static onboarding surface must not overflow horizontally');
    if (fixture.surface === 'desktop-onboarding') {
      assert(summary.journeyState?.startsWith('desktop.'), 'Desktop onboarding must declare a desktop journey state');
      assert.equal(summary.steps, 3, 'Desktop onboarding must keep its three-step progress list');
      assert.equal(summary.hasRecoveryInput, false, 'Desktop onboarding must not reintroduce recovery inputs');
    } else {
      assert.equal(summary.journeyState, 'browser.' + fixture.state.state.replaceAll('-', '_'), 'Browser task must declare its exact journey state');
      assert(summary.statusText.length > 10, 'Browser task must render readable status copy');
      assert.equal(summary.hasNav, false, 'Browser task must not render a second setup wizard');
    }
    return summary;
  }
  throw new Error('Unknown static task surface: ' + fixture.surface);
}
