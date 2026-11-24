import { test, expect } from '@playwright/test';
import { SURFACE_STATES, renderSurfaceState } from './surface-fixtures.mjs';
import { readFileSync } from 'node:fs';
const calls = (page) => page.evaluate(() => window.__surfaceFixture.calls.filter(call => !(call.type === 'ofca.workspace.v1' && ['read', 'draft', 'browser_task', 'desktop_adoption', 'dismiss_browser_task', 'full_presentation_intent'].includes(call.action)) && !(call.type === 'ofca.setup-transfer.v1' && call.action === 'context')));

test('replacing an admitted setup fixture clears the previous browser task', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.mode_choice_full);
  await expect(page.locator('#enable-full')).toBeVisible();
  await page.evaluate(state => window.__surfaceFixture.change(state, true), SURFACE_STATES.start_choice);
  await expect(page.locator('main')).toHaveAttribute('data-task', 'start');
  await expect(page.locator('#start-preview')).toBeVisible();
  await expect(page.locator('#enable-full')).toBeHidden();
  expect(await reviewActions(page)).toEqual([]);
});

test('the reauthorization fixture returns to review for the paused mode', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.reauthorization);
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'consent:reauthorization:preview');
  await expect(page.locator('#review-notice')).toBeVisible();
  await expect(page.locator('#enable-preview')).toBeVisible();
  await expect(page.locator('#enable-preview')).toBeDisabled();
});

test('Preview keeps its status and Full review action visible in the reserved frames', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.preview);
  await expect(page.locator('#journey-title')).toBeVisible();
  await expect(page.locator('#journey-title')).toHaveText('Ready');
  await expect(page.locator('#journey-primary')).toBeVisible();
  await page.locator('#journey-primary').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.open-surface'))
    .toEqual([{ type: 'ofca.ui.open-surface', surface: 'setup', section: 'full', requestedFull: true }]);
});

test('loading setup never records approvals or requests browser access', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, permissionGranted: false });
  await expect(page.locator('#start-choice')).toBeVisible();
  expect(await page.evaluate(() => window.__surfaceFixture.calls.some(call => call.type === 'ofca.workspace.v1' && call.action === 'full_presentation_intent'))).toBe(false);
  await page.locator('#start-preview').click();
  await expect(page.locator('#terms-accepted')).not.toBeChecked();
  await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
  await expect(page.locator('#enable-preview')).toBeDisabled();
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
  expect(await page.evaluate(() => window.__surfaceFixture.calls.filter(call => call.type === 'ofca.workspace.v1' && call.action === 'full_presentation_intent').every(call => call.requested === false))).toBe(true);
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await expect(page.locator('#risk-acknowledged')).toBeEnabled();
  await expect(page.locator('#enable-preview')).toBeDisabled();
  await page.locator('#risk-acknowledged').check();
  await expect(page.locator('#enable-preview')).toBeEnabled();
  await expect(page.locator('#mode-permission-note')).toBeVisible();
  expect((await calls(page)).filter((call) => !call.type.endsWith('.status')).map((call) => call.type))
    .toEqual(['ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software']);
  expect((await calls(page)).some((call) => call.type === 'permission')).toBe(false);
});

test('consent decisions stay on one page and use three distinct gestures', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.software_activation);
  await page.locator('#start-preview').click();
  const enable = page.locator('#enable-preview');
  await expect(enable).toBeVisible();
  await expect(enable).toBeDisabled();
  await page.locator('#risk-acknowledged').check();
  await expect.poll(async () => (await calls(page)).filter((call) => call.type.endsWith('acknowledge-risk')).length).toBe(1);
  await expect(page.locator('#terms-accepted')).not.toBeChecked();
  await expect(enable).toBeDisabled();
  await page.locator('#terms-accepted').check();
  await expect(enable).toBeEnabled();
  const decisions = async () => (await calls(page)).filter((call) => !call.type.endsWith('.status')).map((call) => call.type);
  expect(await decisions()).toEqual(['ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.accept-terms',
    'ofca.legal-activation.activate-software']);
  await enable.click();
  await expect(page.locator('#preview-metrics')).toBeVisible();
  expect((await decisions()).at(-1)).toBe('ofca.legal-activation.choose-mode');
  expect((await decisions()).includes('permission')).toBe(false);
});

const reviewSnapshot = (page) => page.evaluate(async () => {
  const { result: workspace } = await chrome.runtime.sendMessage({ type: 'ofca.workspace.v1', action: 'read' });
  const { status } = await chrome.runtime.sendMessage({ type: 'ofca.ui.status' });
  const { result: legal } = await chrome.runtime.sendMessage({ type: 'ofca.legal-activation.status' });
  return { workspace, consent: status.consent, flow: legal.flow,
    choice: JSON.parse(sessionStorage.getItem('onboarding-mode-review-v1')) };
});

const reviewActions = (page) => page.evaluate(() => window.__surfaceFixture.calls.filter((call) =>
  !call.type.endsWith('.status') && !(call.type === 'ofca.workspace.v1'
    && ['read', 'browser_task', 'desktop_adoption'].includes(call.action))));

for (const ready of [true, false]) test(`Preview review link switches to Full with ${ready ? 'validated' : 'outstanding'} prerequisites`, async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, persistReview: true, permissionGranted: false });
  await page.getByRole('button', { name: 'Continue with Preview' }).click();
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  if (ready) {
    await page.locator('#risk-acknowledged').check();
    await expect(page.locator('#enable-preview')).toBeEnabled();
  } else await expect(page.locator('#enable-preview')).toBeDisabled();
  const before = await reviewSnapshot(page);
  expect(before.choice.mode).toBe('preview');
  const previousActionCount = (await reviewActions(page)).length;
  const { journey_id, draft_scope } = before.workspace;

  await page.getByRole('button', { name: 'Review Full analytics', exact: true }).click();
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(page.locator('#desktop-entry-title')).toBeFocused();
  await expect(page.locator('#mode-choice')).toBeHidden();
  const after = await reviewSnapshot(page);
  expect(after.workspace).toMatchObject({ journey_id, draft_scope, draft: { full_checked: true } });
  expect(after.choice).toEqual({ ...before.choice, mode: 'full' });
  expect(after.consent).toEqual(before.consent);
  expect(after.flow).toEqual(before.flow);
  expect((await reviewActions(page)).slice(previousActionCount)).toEqual([
    { type: 'ofca.workspace.v1', action: 'full_presentation_intent', scope_id: draft_scope.scope_id, requested: true },
    { type: 'ofca.workspace.v1', action: 'draft', scope_id: draft_scope.scope_id, draft: { ...before.workspace.draft, full_checked: true } },
  ]);

  await page.reload();
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => window.__surfaceFixture.change({ browserTask: true }));
  await expect(page.locator('#full-disclosure')).toBeVisible();
  await expect(page.locator('#preview-disclosure')).toBeHidden();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  if (ready) await expect(page.locator('#enable-full')).toBeEnabled();
  else {
    await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
    await expect(page.locator('#enable-full')).toBeDisabled();
  }
  const admitted = await reviewSnapshot(page);
  expect(admitted.consent).toEqual(before.consent);
  expect(admitted.flow).toEqual(before.flow);
  expect(await reviewActions(page)).toEqual([]);

  const replacement = { ...draft_scope, scope_id: '22222222-2222-4222-8222-222222222223' };
  await page.evaluate((draftScope) => window.__surfaceFixture.change({ draftScope, browserTask: false }), replacement);
  await expect(page.locator(ready ? '#preview-disclosure' : '#start-choice')).toBeVisible();
  await expect(page.locator('#desktop-entry')).toBeHidden();
  const replaced = await reviewSnapshot(page);
  expect(replaced.workspace).toMatchObject({ draft_scope: replacement, draft: { full_checked: false } });
  expect(replaced.choice).toBeNull();
  expect(replaced.consent).toEqual(before.consent);
  expect(replaced.flow).toEqual(before.flow);
  expect(await reviewActions(page)).toEqual([]);
});

test('review navigation and Keep Preview preserve an existing Preview authorization', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview_complete, authorizationEventId: 'fixture-preview-receipt' });
  const before = await reviewSnapshot(page);
  await page.getByRole('button', { name: 'Review Full analytics', exact: true }).click();
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => window.__surfaceFixture.change({ browserTask: true }));
  await expect(page.locator('#full-disclosure')).toBeVisible();
  expect((await reviewSnapshot(page)).consent).toEqual(before.consent);
  await page.getByRole('button', { name: 'Keep Preview', exact: true }).click();
  await expect(page.locator('#preview-metrics')).toBeVisible();
  const after = await reviewSnapshot(page);
  expect(after.consent).toEqual(before.consent);
  expect(after.flow).toEqual(before.flow);
  const scope_id = before.workspace.draft_scope.scope_id;
  expect(await reviewActions(page)).toEqual([
    { type: 'ofca.workspace.v1', action: 'full_presentation_intent', scope_id, requested: true },
    { type: 'ofca.workspace.v1', action: 'draft', scope_id, draft: { ...before.workspace.draft, full_checked: true } },
    { type: 'ofca.workspace.v1', action: 'full_presentation_intent', scope_id, requested: false },
    { type: 'ofca.workspace.v1', action: 'draft', scope_id, draft: before.workspace.draft },
    { type: 'ofca.workspace.v1', action: 'dismiss_browser_task' },
  ]);
  await page.reload();
  await expect(page.locator('#preview-metrics')).toBeVisible();
  expect((await reviewSnapshot(page)).consent).toEqual(before.consent);
  expect(await reviewActions(page)).toEqual([]);
});

test('setup validation must succeed before the consent mode action becomes available', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, validationRejected: true });
  await page.locator('#start-preview').click();
  const feedback = page.locator('#feedback');
  await expect(feedback).toHaveRole('status');
  await expect(feedback).toHaveAttribute('aria-live', 'polite');
  await expect(feedback).toHaveCSS('display', 'block');
  await expect(feedback).toHaveCSS('visibility', 'visible');
  await page.locator('#terms-accepted').check();
  await page.locator('#risk-acknowledged').check();
  const retry = page.locator('#retry-validation');
  await expect(retry).toBeVisible();
  await expect(feedback).toHaveText('The setup check didn’t finish.');
  await expect(page.locator('#enable-preview')).toBeDisabled();
  const before = (await reviewSnapshot(page)).flow;
  expect(acknowledgmentCount(before)).toBe(2);
  expect(before.stage).toBe('pre_mode');
  expect((await calls(page)).filter(call => call.type.endsWith('activate-software')).length).toBe(1);
  await page.evaluate(() => window.__surfaceFixture.change({ validationRejected: false }));
  await retry.click();
  await expect(page.locator('#enable-preview')).toBeEnabled();
  await expect(feedback).toBeEmpty();
  expect((await reviewSnapshot(page)).flow).toEqual({ ...before, stage: 'mode_selection' });
  expect((await calls(page)).filter(call => /accept-terms$|acknowledge-risk$/.test(call.type))).toHaveLength(2);
  expect((await calls(page)).filter(call => call.type.endsWith('activate-software')).length).toBe(2);
  expect((await calls(page)).some(call => call.type.endsWith('choose-mode') || call.type === 'permission')).toBe(false);
});

const acknowledgmentCount = (flow) => [flow.terms_event_id, flow.risk_event_id].filter(Boolean).length;

test('a lost validation reply announces no failure when the saved review is ready', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation,
    lostResponse: 'ofca.legal-activation.activate-software' });
  await page.locator('#start-preview').click();
  await page.locator('#terms-accepted').check();
  await page.locator('#risk-acknowledged').check();
  await expect(page.locator('#enable-preview')).toBeEnabled();
  await expect(page.locator('#feedback')).toBeEmpty();
  await expect(page.locator('#retry-validation')).toBeHidden();
  const saved = await reviewSnapshot(page);
  expect(acknowledgmentCount(saved.flow)).toBe(2);
  expect(saved.flow.stage).toBe('mode_selection');
  expect(saved.consent.mode).toBe('off');
  expect((await calls(page)).filter(call => !call.type.endsWith('.status')).map(call => call.type)).toEqual([
    'ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software',
  ]);
});

for (const mode of ['revoked', 'paused']) test(`Not now leaves a Preview re-entry action during ${mode} agreement`, async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, mode,
    resume: mode === 'paused' ? 'preview' : null, reauthorization: mode === 'paused', permissionGranted: false });
  const navigations = [];
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations.push(frame.url()); });
  if (mode === 'revoked') await page.locator('#start-preview').click();
  await expect(page.locator('#preview-disclosure')).toBeVisible();
  await page.locator('#terms-accepted').check();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  const before = await reviewSnapshot(page);
  expect(acknowledgmentCount(before.flow)).toBe(1);
  await page.locator('#not-now-preview').click();
  await expect(page.locator('#mode-choice')).toBeHidden();
  const reentry = page.locator('#journey-primary');
  await expect(reentry).toHaveText(mode === 'revoked' ? 'Choose analytics mode' : 'Review changes');
  await expect(reentry).toBeEnabled();
  expect((await reviewSnapshot(page)).consent).toEqual(before.consent);
  await reentry.click();
  if (mode === 'revoked') await page.locator('#start-preview').click();
  await expect(page.locator('#preview-disclosure')).toBeVisible();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
  await expect(page.locator('#enable-preview')).toBeDisabled();
  const reopened = await reviewSnapshot(page);
  expect(reopened.flow).toEqual(before.flow);
  expect(reopened.consent).toEqual(before.consent);
  expect((await calls(page)).filter(call => !call.type.endsWith('.status')).map(call => call.type)).toEqual([
    'ofca.legal-activation.accept-terms',
  ]);
  await page.locator('#risk-acknowledged').check();
  await expect(page.locator('#enable-preview')).toBeEnabled();
  const ready = await reviewSnapshot(page);
  expect(acknowledgmentCount(ready.flow)).toBe(2);
  expect(ready.flow.terms_event_id).toBe(before.flow.terms_event_id);
  expect(ready.consent).toEqual(before.consent);
  expect((await calls(page)).filter(call => !call.type.endsWith('.status')).map(call => call.type)).toEqual([
    'ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software',
  ]);
  expect(navigations).toHaveLength(0);
});

test('acknowledgment gestures remain unchecked while a workspace refresh is pending', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.software_activation);
  await page.locator('#start-preview').click();
  const terms = page.locator('#terms-accepted'), risk = page.locator('#risk-acknowledged');
  await expect(terms).toBeEnabled();
  await page.evaluate(async () => {
    window.__surfaceFixture.state.holdWorkspaceRead = true;
    await chrome.storage.session.set({ browser_task_entry_v1: null });
  });
  await page.waitForFunction(() => typeof window.__releaseWorkspaceRead === 'function');
  await terms.press('Space');
  await expect(terms).not.toBeChecked();
  await risk.scrollIntoViewIfNeeded();
  const riskBox = await risk.boundingBox();
  await page.mouse.click(riskBox.x + riskBox.width / 2, riskBox.y + riskBox.height / 2);
  await expect(risk).not.toBeChecked();
  await expect(terms).toHaveAttribute('aria-disabled', 'true');
  await expect(risk).toHaveAttribute('aria-disabled', 'true');
  const pending = await page.evaluate(async () => (await chrome.runtime.sendMessage({ type: 'ofca.legal-activation.status' })).result.flow);
  expect(acknowledgmentCount(pending)).toBe(0);
  expect((await calls(page)).every(call => call.type.endsWith('.status'))).toBe(true);
  await page.evaluate(() => window.__releaseWorkspaceRead());
  await expect(terms).toBeEnabled();
  await expect(risk).toBeEnabled();
  await terms.check();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await risk.check();
  await expect(page.locator('#enable-preview')).toBeEnabled();
  const saved = await reviewSnapshot(page);
  expect(acknowledgmentCount(saved.flow)).toBe(2);
  expect(saved.consent.mode).toBe('off');
  expect((await calls(page)).filter(call => !call.type.endsWith('.status')).map(call => call.type)).toEqual([
    'ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software',
  ]);
});

test('permission refusal stays off and never submits a mode choice', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.mode_choice, permissionGranted: false });
  await page.locator('#enable-preview').click();
  await expect(page.locator('#feedback')).toContainText('Site access was not allowed');
  const messages = await calls(page);
  expect(messages.find((call) => call.type === 'permission').userGesture).toBe(true);
  expect(messages.some((call) => call.type === 'ofca.legal-activation.choose-mode')).toBe(false);
  expect(await page.evaluate(() => window.__surfaceFixture.state.mode)).toBe('off');
});

for (const { width, height, theme, mode } of [
  { width: 1440, height: 900, theme: 'light', mode: 'preview' },
  { width: 390, height: 844, theme: 'dark', mode: 'full' },
]) test(`consent keyboard order and re-entry at ${width} ${theme}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, browserTask: mode === 'full',
    persistReview: true, lostResponse: 'ofca.legal-activation.accept-terms', permissionGranted: false, permissionOutcome: 'granted' });
  if (mode === 'preview') await page.locator('#start-preview').click();
  const enable = page.locator(`#enable-${mode}`);
  await expect(enable).toBeDisabled();
  const disclosure = page.locator(`#${mode}-disclosure`);
  await expect(disclosure).toBeVisible();
  await expect(disclosure.getByRole('link', { name: 'Extension Privacy Notice' })).toHaveAttribute('href', /\/privacy$/);
  await expect(disclosure.getByRole('link', { name: 'Creator platform and data risks' })).toHaveAttribute('href', /\/risk$/);
  await disclosure.getByRole('link', { name: 'Creator platform and data risks' }).focus();
  await page.keyboard.press('Tab');
  await expect(page.locator('#terms-accepted')).toBeFocused();
  await page.keyboard.press('Space');
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await page.reload();
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await expect(page.locator('#terms-accepted')).toBeChecked();
  await expect(page.locator('#risk-acknowledged')).not.toBeChecked();
  await expect(enable).toBeDisabled();
  await page.locator('#terms-accepted').focus();
  for (const id of ['terms-link', 'risk-acknowledged']) {
    await page.keyboard.press('Tab');
    await expect(page.locator(`#${id}`)).toBeFocused();
  }
  await page.keyboard.press('Space');
  await expect(page.locator('#risk-recorded')).toHaveText('Recorded');
  await expect(enable).toBeEnabled();
  for (const id of ['risk-link', `enable-${mode}`]) {
    await page.keyboard.press('Tab');
    await expect(page.locator(`#${id}`)).toBeFocused();
  }
  const boxes = await Promise.all([disclosure, page.locator('#separate-choices-disclosure'),
    page.locator('#pre-mode'), page.locator('#mode-permission-note'), enable].map((locator) => locator.boundingBox()));
  expect(boxes.every((box, index) => box.width > 0 && box.height > 0
    && (index === 0 || boxes[index - 1].y + boxes[index - 1].height <= box.y))).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath(`${width}-${theme}-${mode}.png`), fullPage: true });
  await page.keyboard.press('Enter');
  await expect(page.locator('#journey-title')).toBeVisible();
  const messages = await calls(page);
  expect(messages.filter((call) => call.type === 'permission' || call.type.endsWith('choose-mode'))
    .map((call) => call.type)).toEqual(['permission', 'ofca.legal-activation.choose-mode']);
  expect(messages.filter((call) => call.type === 'permission').map((call) => call.userGesture)).toEqual([true]);
  expect(messages.filter((call) => call.type.endsWith('choose-mode')).map((call) => call.mode)).toEqual([mode]);
  expect(messages.some((call) => call.type.endsWith('accept-terms'))).toBe(false);
});

for (const receipt of ['current', 'missing', 'mismatched', 'previous', 'needs-review']) {
  test(`lost Full response recovers only with a current matching receipt: ${receipt}`, async ({ page }) => {
    await renderSurfaceState(page, { ...SURFACE_STATES.full_review, authorizationEventId: 'fixture-preview-receipt',
      lostResponse: 'ofca.legal-activation.choose-mode', modeReceipt: receipt });
    const enable = page.locator('#enable-full');
    await expect(enable).toBeEnabled();
    await enable.click();
    const feedback = page.locator('#feedback');
    if (receipt === 'current') {
      await expect(page.locator('main')).toHaveAttribute('data-task', 'browser-access-ready');
      await expect(feedback).toBeEmpty();
      const { consent, flow } = await reviewSnapshot(page);
      expect(consent.authorization_event_id).toBe('fixture-full-receipt');
      expect(flow.completed_event_id).toBe(consent.authorization_event_id);
    } else {
      await expect(feedback).toHaveText('That did not complete.');
    }
    expect((await calls(page)).filter((call) => call.type.endsWith('choose-mode')).map((call) => call.mode)).toEqual(['full']);
  });
}

for (const permissionOutcome of ['denied', 'dismissed']) test(`Full consent keeps Preview on simulated ${permissionOutcome} access`, async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.full_review, permissionGranted: false, permissionOutcome });
  await expect(page.locator('#full-secondary')).toHaveText('Keep Preview');
  await page.locator('#enable-full').click();
  await expect(page.locator('#feedback')).toContainText('Site access was not allowed');
  await expect(page.locator('#terms-recorded')).toHaveText('Recorded');
  await expect(page.locator('#risk-recorded')).toHaveText('Recorded');
  expect((await calls(page)).some((call) => call.type.endsWith('choose-mode'))).toBe(false);
  expect(await page.evaluate(() => window.__surfaceFixture.state.mode)).toBe('preview');
  await page.locator('#full-secondary').click();
  await expect(page.locator('#preview-metrics')).toBeVisible();
});

test('a permission response cannot complete a replaced consent review', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.mode_choice, permissionGranted: false, permissionOutcome: 'granted', holdPermission: true });
  await page.locator('#enable-preview').click();
  await page.waitForFunction(() => typeof window.__releasePermission === 'function');
  await page.evaluate(() => window.__surfaceFixture.change({ transactionId: '66666666-6666-4666-8666-666666666666' }));
  await expect.poll(async () => (await calls(page)).filter((call) => call.type.endsWith('.status')).length).toBeGreaterThan(2);
  await page.evaluate(() => window.__releasePermission());
  await expect(page.locator('#feedback')).toContainText('Setup changed');
  expect((await calls(page)).some((call) => call.type.endsWith('choose-mode'))).toBe(false);
});

test('Preview finishes setup without pairing or desktop requests', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.mode_choice);
  await page.locator('#enable-preview').click();
  await expect(page.locator('#journey-title')).toHaveText('Preview is ready');
  await expect(page.locator('[data-step="connect"]')).toBeHidden();
  await expect(page.locator('#companion-pairing')).toBeHidden();
  expect((await calls(page)).some((call) => ['pair', 'tab'].includes(call.type))).toBe(false);
});

test('Preview counts return after required review or site access without changing consent', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.preview_complete);
  const counts = page.locator('#preview-metrics');
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  await page.locator('#journey-primary').click();
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(counts).toBeHidden();
  await page.locator('#desktop-back').click();
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  await page.evaluate(() => window.__surfaceFixture.change({ phase: 'permission_required' }));
  await expect(page.locator('#access-card')).toBeVisible();
  await expect(counts).toBeHidden();
  await page.evaluate(() => window.__surfaceFixture.change({ phase: 'preview' }));
  await expect(counts).toBeVisible();
  await expect(page.locator('#messages-count')).toHaveText('128');
  expect(await page.evaluate(() => window.__surfaceFixture.state.mode)).toBe('preview');
  expect((await calls(page)).every((call) => call.type.endsWith('.status'))).toBe(true);
});

test('deletion is scoped and cancellation makes no deletion request', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.delete_confirmation);
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Messages already stored by the desktop app are not deleted');
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect((await calls(page)).some((call) => call.type === 'ofca.ui.delete-local-data')).toBe(false);
  await page.locator('#delete-local-data').click();
  await dialog.getByRole('button', { name: 'Delete extension data', exact: true }).click();
  await expect.poll(async () => (await calls(page)).filter((call) => call.type === 'ofca.ui.delete-local-data').length).toBe(1);
});

test('losing runtime access hides the desktop handoff and offers recovery', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.pairing_compare);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => { window.__surfaceFixture.change({ runtimeUnavailable: true }); window.dispatchEvent(new Event('focus')); });
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#runtime-title')).toHaveText('Can’t check the extension right now');
  await expect(page.locator('#desktop-entry')).toBeHidden();
  await expect(page.locator('#retry-runtime')).toHaveCount(0);
  await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: false }));
  await expect(page.locator('#runtime-unavailable')).toBeHidden();
});

test('status badge and next action reflect a confirmed sleeping browser tab', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.full_tab_sleeping, surface: 'popup' });
  await expect(page.locator('#journey-title')).toHaveText('Browser tab paused');
  await expect(page.locator('#journey-badge')).toHaveText('Browser tab paused');
  await expect(page.locator('#analysis-status')).not.toHaveText('Ready');
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
});

test('the setup sleeping action focuses the existing creator site tab', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_tab_sleeping);
  await page.evaluate(() => {
    chrome.tabs.query = async () => [{ id: 5, active: false }];
    chrome.tabs.update = async (id, update) => { window.__surfaceFixture.calls.push({ type: 'activate-tab', id, ...update }); };
  });
  await expect(page.locator('#desktop-entry-title')).toHaveText('Updates paused');
  await expect(page.locator('#desktop-entry-body')).toHaveText('Your browser paused the creator site tab.');
  expect((await calls(page)).some(({ type }) => ['activate-tab', 'tab'].includes(type))).toBe(false);
  await page.getByRole('button', { name: 'Open creator account', exact: true }).click();
  expect((await calls(page)).filter(({ type }) => ['activate-tab', 'tab'].includes(type))).toEqual([{ type: 'activate-tab', id: 5, active: true }]);
});

test('the pinned popup keeps its Full summary, primary action and disclosure', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#journey-title')).toHaveText('Ready');
  await expect(page.locator('#journey-body')).toHaveText('Keep your creator tab open so new messages can arrive.');
  const action = page.locator('#journey-primary');
  await expect(action).toHaveText('Continue in the desktop app');
  await expect(page.locator('footer')).toContainText('Use only with a creator account you are authorized to analyze.');
  await expect(page.locator('footer')).toContainText(/Independent software\. It is not affiliated with or endorsed by /u);
  await expect(page.locator('footer #privacy-link')).toHaveText('Extension Privacy Notice');
  expect((await action.boundingBox()).y).toBeLessThan((await page.locator('footer').boundingBox()).y);
  expect(await page.locator('body').evaluate((body) => body.scrollHeight)).toBeGreaterThanOrEqual(await page.locator('body').evaluate((body) => body.clientHeight));
  expect(await page.evaluate(() => document.scrollingElement === document.documentElement)).toBe(true);
  expect(await page.locator('main, .popup-content').evaluateAll((nodes) => nodes.some(node => {
    const style = getComputedStyle(node);
    return ['auto', 'scroll'].includes(style.overflowY) && node.scrollHeight > node.clientHeight;
  }))).toBe(false);
});

test('unknown extension connection does not ask for another creator sign-in', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.extension_connection_unknown, surface: 'popup' });
  await expect(page.locator('#journey-title')).toHaveText('Status unavailable');
  await expect(page.locator('#journey-badge')).toHaveText('Status unavailable');
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
  await expect(page.locator('#journey-card')).not.toContainText('Sign in to your creator account');
});

test('consent revocation shows analytics off without restoring it automatically', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_access_revoked);
  await expect(page.locator('#journey-title')).toHaveText('Analytics off');
  await expect(page.locator('#journey-badge')).toHaveText('');
  await expect(page.locator('#journey-icon .onboarding-icon')).toHaveAttribute('data-icon', 'pause');
  await expect(page.locator('#mode-choice')).toBeHidden();
  await expect(page.locator('#journey-primary')).toHaveText('Choose analytics mode');
  await expect(page.locator('#journey-primary')).toHaveAttribute('data-action', 'choose_mode');
  expect((await calls(page)).some(call => call.type === 'permission')).toBe(false);
});

test('refused permission preserves the blocked state and never claims readiness', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.permission_required, permissionGranted: false });
  await expect(page.locator('#access-card')).toBeVisible();
  await page.locator('#restore-access').click();
  await expect(page.locator('#feedback')).toContainText('Site access was not allowed');
  await expect(page.locator('#journey-badge')).toHaveText('Needs access');
  expect((await calls(page)).some((call) => call.type === 'ofca.ui.transition')).toBe(false);
});

test('Full handoff keeps one nonvisual status announcement as runtime facts change', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_required);
  const announcement = page.locator('#step-summary');
  await expect(announcement).toHaveAttribute('role', 'status');
  await expect(announcement).toHaveAttribute('aria-live', 'polite');
  await expect(announcement).toHaveAttribute('aria-atomic', 'true');
  await expect(announcement).toHaveClass(/visually-hidden/);
  await expect(announcement).toHaveText('Continue in the desktop app');
  await page.evaluate(() => {
    window.__announcementChanges = 0;
    new MutationObserver(() => { window.__announcementChanges++; })
      .observe(document.querySelector('#step-summary'), { childList: true, characterData: true, subtree: true });
    window.__surfaceFixture.change({ commercial: 'active', ready: true });
  });
  await expect(announcement).toHaveText('Continue in the desktop app');
  await expect(page.locator('#desktop-entry')).toBeVisible();
  const initial = await page.evaluate(() => window.__announcementChanges);
  await page.evaluate(() => window.__surfaceFixture.change({}));
  await expect.poll(async () => page.evaluate(() => window.__announcementChanges)).toBe(initial);
});

test('popup announces a changed connection status only once', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  const announcement = page.locator('#popup-status-announcement');
  await expect(announcement).toHaveAttribute('role', 'status');
  await expect(announcement).toHaveAttribute('aria-live', 'polite');
  await expect(announcement).toHaveAttribute('aria-atomic', 'true');
  await expect(announcement).toHaveText(/Ready/);
  await page.evaluate(() => {
    window.__popupAnnouncements = 0;
    new MutationObserver(() => { window.__popupAnnouncements++; })
      .observe(document.querySelector('#popup-status-announcement'), { childList: true, characterData: true, subtree: true });
    window.__surfaceFixture.change({ reachable: false });
  });
  await expect(announcement).toContainText('Not connected');
  const changes = await page.evaluate(() => window.__popupAnnouncements);
  await page.evaluate(() => window.__surfaceFixture.change({}));
  await expect.poll(async () => page.evaluate(() => window.__popupAnnouncements)).toBe(changes);
});

test('popup reports an unavailable status without claiming reconnection is still running', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.runtime_unavailable, surface: 'popup' });
  await expect(page.locator('#runtime-title')).toHaveText('Extension status is unavailable');
  await expect(page.locator('#retry-runtime')).toHaveCount(0);
});

test('options keeps its unavailable heading without offering a retry action', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.connection, surface: 'options', runtimeUnavailable: true });
  await expect(page.locator('#runtime-unavailable')).toBeVisible();
  await expect(page.locator('#runtime-title')).toHaveText('Extension status is unavailable');
  await expect(page.locator('#runtime-unavailable button, #runtime-unavailable p')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Try again', exact: true })).toHaveCount(0);
});

test('options describes reconnection only while the current port recovery is running', async ({ page }) => {
  await page.clock.install();
  await renderSurfaceState(page, { ...SURFACE_STATES.connection, runtimeUnavailable: true, portUnavailable: true });
  const title = page.locator('#runtime-title');
  await expect(title).toHaveText('The extension is reconnecting');
  await page.clock.runFor(1700);
  await expect(title).toHaveText('Extension status is unavailable');
  await expect(page.locator('#runtime-unavailable button')).toHaveCount(0);
});

test('popup uses pause and resume commands without changing the selected mode', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview, surface: 'popup' });
  await page.locator('#pause').click();
  await expect(page.locator('#mode-label')).toHaveText('Analytics paused');
  await page.locator('#journey-primary').click();
  await expect(page.locator('#mode-label')).toHaveText('Preview on');
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition').map((call) => call.mode))
    .toEqual(['pause', 'resume']);
});

for (const [name, state] of Object.entries(SURFACE_STATES).filter(([, value]) => value.surface === 'popup')) {
  test(`popup keyboard order follows its content layout (${name})`, async ({ page }) => {
    await renderSurfaceState(page, state);
    await expect(page.locator('#journey-primary')).toBeVisible();
    const stops = [];
    for (let index = 0; index < 12; index++) {
      await page.keyboard.press('Tab');
      const stop = await page.evaluate(() => {
        const node = document.activeElement;
        if (!node || node === document.body) return null;
        const content = node.closest('.popup-content > [data-region-content]');
        return { key: node.id || node.outerHTML, top: content ? node.getBoundingClientRect().top - content.getBoundingClientRect().top : null };
      });
      if (!stop || stops.some((seen) => seen.key === stop.key)) break;
      stops.push(stop);
    }
    const tops = stops.filter((stop) => stop.top !== null).map((stop) => stop.top);
    expect(tops.length).toBeGreaterThan(0);
    expect(tops).toEqual([...tops].sort((a, b) => a - b));
  });
}

test('while the desktop app controls this browser, the popup explains where pause moved', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.desktop_controlled);
  await expect(page.locator('#pause')).toBeHidden();
  await expect(page.locator('#desktop-control-note')).toBeVisible();
});

test('the popup shows no desktop pause note while it owns pause itself', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#desktop-control-note')).toBeHidden();
});

test('persistent setup sends paused Full to desktop control instead of duplicating Resume', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.desktop_controlled_paused, surface: 'setup' });
  await expect(page.locator('#pause')).toBeHidden();
  await expect(page.locator('#open-desktop')).toHaveText('Open desktop app');
  await page.locator('#open-desktop').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition')).toEqual([]);
});

test('paused Preview keeps Resume in the extension even with an existing desktop channel', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.desktop_controlled_paused, surface: 'setup', resume: 'preview' });
  await expect(page.locator('#journey-primary')).toHaveText('Resume');
  await page.locator('#journey-primary').click();
  await expect.poll(async () => (await calls(page)).filter((call) => call.type === 'ofca.ui.transition').length).toBe(1);
});

test('the compact setup window for the desktop app shows only its task', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.pairing_required, browserTask: true });
  await expect(page.locator('#journey-title')).toHaveText('Browser access is ready');
  await expect(page.locator('main')).toHaveAttribute('data-requirement', 'browser-access-ready');
  await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
});

test('while the desktop app controls a paused browser, resume points to the desktop app', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.desktop_controlled_paused);
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
  await page.locator('#journey-primary').click();
  expect((await calls(page)).filter((call) => call.type === 'ofca.ui.transition')).toEqual([]);
  expect((await calls(page)).filter(call => call.type === 'ofca.ui.open-surface')).toEqual([{ type: 'ofca.ui.open-surface', surface: 'setup', section: 'full' }]);
});

for (const [name, visible] of [['connection', true], ['connection_desktop_controlled', false]]) {
  test(`options ${visible ? 'offers' : 'leaves to the desktop app'} forgetting the connection (${name})`, async ({ page }) => {
    await renderSurfaceState(page, SURFACE_STATES[name]);
    await expect(page.locator('#forget-companion')).toBeVisible({ visible });
  });
}

test('history recovery does not promise collection is ready after waking the tab', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.connection, sleeping: true });
  await expect(page.locator('#history-health')).toHaveText('Your browser paused the creator site. Open the tab to continue.');
  await expect(page.locator('#history-health')).not.toContainText('automatically');
});

test('options offers no second pause control', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.preview, surface: 'options' });
  await expect(page.locator('#pause')).toHaveCount(0);
});

test('settings tracks the separate desktop history choice, including withdrawal with a browser grant', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.connection);
  const action = page.locator('#history');
  const status = page.locator('#history-status');
  await expect(action).toBeHidden();
  await expect(status).toBeEmpty();
  await page.evaluate(() => window.__surfaceFixture.change({ historyChoice: 'choice-A' }));
  await expect(action).toBeVisible();
  await page.evaluate(() => window.__surfaceFixture.change({ historyPermission: true }));
  await expect(status).toHaveText('Browser access allowed.');
  await expect(action).toBeHidden();
  await page.evaluate(() => window.__surfaceFixture.change({ historyChoice: null }));
  await expect(status).toBeEmpty();
  await expect(action).toBeHidden();
});

test('options history permission uses the approved copy and preserves denied access state', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: 'history', historyChoice: 'choice-A', permissionGranted: false });
  await expect(page.locator('#history-status')).toHaveText('You turned on message history in the desktop app. Allow the extension to read your older messages.');
  await expect(page.locator('#history')).toHaveText('Allow browser access');
  await expect(page.locator('#history')).toBeVisible();
  await expect(page.locator('#history-row')).not.toHaveClass(/reserved-empty/u);
  await page.locator('#history').click();
  await expect(page.locator('#feedback')).toContainText('Message history access was not allowed');
  await expect(page.locator('#history-status')).toHaveText('You turned on message history in the desktop app. Allow the extension to read your older messages.');
  const permission = (await calls(page)).find(call => call.type === 'permission');
  expect(permission.request).toEqual({ permissions: ['webRequest'], origins: ['https://onlyfans.com/*'] });
  expect(permission.userGesture).toBe(true);
});

for (const width of [1440, 390]) {
  test(`history handoff checking uses a busy placeholder and no tall empty row at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => { location.hash = '#history'; });
    const rendering = renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: undefined, historyChoice: 'choice-A', initialRequestHold: 'ofca.ui.status' });
    await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
    await expect(page.locator('#loading')).toBeHidden();
    await expect(page.locator('#connection')).toBeVisible();
    const status = page.locator('#history-status');
    const state = status.locator('..');
    await expect(state).toHaveAttribute('aria-busy', 'true');
    await expect(status).toBeEmpty();
    const placeholder = await status.evaluate((node) => ({
      content: getComputedStyle(node, '::before').content,
      height: parseFloat(getComputedStyle(node, '::before').height),
      width: parseFloat(getComputedStyle(node, '::before').width),
      lineHeight: parseFloat(getComputedStyle(node.parentElement).lineHeight),
    }));
    expect(placeholder.content).not.toBe('none');
    expect(placeholder.height).toBeLessThanOrEqual(placeholder.lineHeight);
    expect(placeholder.width).toBeGreaterThan(0);
    const rowCopy = await state.evaluate((node) => ({ height: node.getBoundingClientRect().height,
      lineHeight: parseFloat(getComputedStyle(node).lineHeight) }));
    expect(rowCopy.height).toBeLessThanOrEqual(rowCopy.lineHeight * 2 + 1);
    await page.evaluate(() => window.__releaseInitialRequest());
    await rendering;
    await expect(state).not.toHaveAttribute('aria-busy', 'true');
    await expect(status).toHaveText('You turned on message history in the desktop app. Allow the extension to read your older messages.');
  });
}

for (const [name, state] of [['connection', SURFACE_STATES.connection], ['manage', SURFACE_STATES.manage],
  ['permission_required', { ...SURFACE_STATES.connection, phase: 'permission_required' }]]) {
  test(`options frames show their whole cards (${name})`, async ({ page }) => {
    await renderSurfaceState(page, state);
    await expect(page.locator('#restore-access')).toBeVisible({ visible: name === 'permission_required' });
    for (const width of [1280, 601, 600, 390, 360, 359, 320]) {
      await page.setViewportSize({ width, height: 900 });
      const cut = await page.locator('.options-frame').evaluateAll((frames) => frames.map((frame) => frame.scrollHeight - frame.clientHeight));
      expect(cut, `${width}px`).toEqual([0, 0, 0]);
    }
  });
}

for (const width of [320, 390, 600]) {
  for (const scale of [1.25, 1.5, 2]) {
    test(`connection details status slots stay fixed at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: '' });
      await page.evaluate(async (factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        await document.fonts.ready;
        const sendMessage = chrome.runtime.sendMessage;
        window.__dialogDelivery = {};
        chrome.runtime.sendMessage = async (message) => {
          const response = await sendMessage(message);
          if (message.type === 'ofca.ui.status') Object.assign(response.status.delivery, window.__dialogDelivery);
          return response;
        };
      }, scale);
      await page.locator('#open-connection-details').click();
      const dialog = page.locator('#connection-details-dialog');
      await expect(page.locator('#capture-health + .reserved-empty')).toHaveText('Full analytics could not start. Open the desktop app.');
      const boxes = () => dialog.evaluate((node) => [node, ...node.querySelectorAll('h2, dl, dt, dd, p, form, button')]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return [rect.x, rect.y, rect.width, rect.height];
        }));
      const initial = await boxes();
      const captureStates = [
        [{ startup_error_code: null, capture_drop_counts: {} }, 'No capture issues reported.'],
        [{ startup_error_code: null, capture_drop_counts: { unavailable: Number.MAX_SAFE_INTEGER } },
          `${new Intl.NumberFormat('en-US').format(Number.MAX_SAFE_INTEGER)} updates could not be recorded.`],
        [{ startup_error_code: 'startup_failed', capture_drop_counts: {} },
          'Full analytics could not start. Open the desktop app.'],
      ];
      const historyStates = [
        [{ browser_tab_sleeping: false, history_error_code: null }, ''],
        [{ browser_tab_sleeping: false, history_error_code: 'history_unavailable' }, 'Message history needs attention in the desktop app.'],
        [{ browser_tab_sleeping: true, history_error_code: 'history_unavailable' }, 'Your browser paused the creator site. Open the tab to continue.'],
      ];
      for (const [capture, captureText] of captureStates) for (const [history, historyText] of historyStates) {
        for (const pending of [0, 999, Number.MAX_SAFE_INTEGER]) {
          await page.evaluate((delivery) => {
            window.__dialogDelivery = delivery;
            window.__surfaceFixture.change({});
          }, { ...capture, ...history, pending_entries: pending });
          await expect(page.locator('#capture-health')).toHaveText(captureText);
          await expect(page.locator('#history-health')).toHaveText(historyText);
          await expect(page.locator('#pending-count')).toHaveText(new Intl.NumberFormat('en-US').format(pending));
          expect.soft(await boxes(), JSON.stringify({ capture, history, pending })).toEqual(initial);
        }
      }
      await page.evaluate(() => {
        window.__dialogDelivery = {};
        window.__surfaceFixture.change({});
      });
      await expect(page.locator('#capture-health')).toHaveText('No capture issues reported.');
      await expect(page.locator('#history-health')).toBeEmpty();
      await expect(page.locator('#pending-count')).toHaveText('0');
      expect(await boxes(), 'cleared status').toEqual(initial);
      await dialog.locator('button').scrollIntoViewIfNeeded();
      const close = await dialog.locator('button').boundingBox();
      expect(close.y).toBeGreaterThanOrEqual(0);
      expect(close.y + close.height).toBeLessThanOrEqual(641);
      await dialog.locator('button').click();
      await expect(dialog).not.toBeVisible();
    });

    test(`options state slots stay fixed at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      const rendering = renderSurfaceState(page, { ...SURFACE_STATES.connection, hash: '', initialRequestHold: 'ofca.ui.status' });
      await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
      await page.evaluate(async (factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        await document.fonts.ready;
      }, scale);
      const boxes = () => page.evaluate(() => Object.fromEntries([
        ...document.querySelectorAll('.options-frame, .reserved-status, footer'),
        ...document.querySelectorAll('.options-frame h2, .options-frame h3, .options-frame .settings-row, #open-dashboard, #open-connection-details'),
      ].map((node, index) => {
        const rect = node.getBoundingClientRect();
        return [node.id || `${node.className}:${index}`, [rect.x, rect.y + window.scrollY, rect.width, rect.height]];
      })));
      const loading = await boxes();
      for (const id of ['capture', 'connection', 'data']) await expect(page.locator(`#${id}`)).toBeHidden();
      await page.evaluate(() => window.__releaseInitialRequest());
      await rendering;
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      const loaded = await boxes();
      expect.soft(loaded, 'loading to loaded').toEqual(loading);
      await page.evaluate(() => window.__surfaceFixture.change({ reachable: false }));
      await expect(page.locator('#delivery-status')).toHaveText('Not connected');
      expect.soft(await boxes(), 'connection loss').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ reachable: true }));
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      expect.soft(await boxes(), 'reconnection').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: true }));
      await expect(page.locator('#runtime-unavailable')).toBeVisible();
      expect.soft(await boxes(), 'runtime unavailable').toEqual(loaded);
      await page.evaluate(() => window.__surfaceFixture.change({ runtimeUnavailable: false }));
      await expect(page.locator('#analysis-status')).toHaveText('Ready');
      expect.soft(await boxes(), 'runtime recovered').toEqual(loaded);
    });

    for (const surface of ['popup', 'setup', 'options']) {
      test(`${surface} real feedback keeps task controls anchored at ${width}px and ${scale * 100}% text`, async ({ page }) => {
        await page.setViewportSize({ width, height: 640 });
        await renderSurfaceState(page, surface === 'popup' ? SURFACE_STATES.preview
          : surface === 'setup' ? SURFACE_STATES.activation_required : SURFACE_STATES.manage);
        await page.evaluate((factor) => {
          document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        }, scale);
        const sources = ['../ui/actions.mjs', '../ui/surface-client.mjs', '../setup.js']
          .map((name) => readFileSync(new URL(name, import.meta.url), 'utf8')).join('\n');
        const messages = [...sources.matchAll(/new NoticeError\('([^']+)'\)/gu)].map((match) => match[1]);
        messages.push('That did not complete. Try again.', 'The desktop connection has been removed.', 'Check both boxes to continue.');
        const layout = () => page.evaluate(() => {
          const slot = document.querySelector('.reserved-status');
          const following = document.querySelector('nav.menu') || document.querySelector('footer');
          const rect = following.getBoundingClientRect();
          const feedback = slot.querySelector('.feedback');
          const action = document.querySelector('#open-desktop')?.getBoundingClientRect();
          return { slot: slot.getBoundingClientRect().toJSON(), feedback: feedback.getBoundingClientRect().toJSON(),
            action: action && [action.x, action.y + window.scrollY, action.width, action.height],
            slotLineHeight: parseFloat(getComputedStyle(slot).lineHeight),
            following: [rect.x, rect.y + window.scrollY, rect.width, rect.height] };
        });
        const empty = await layout();
        if (surface === 'setup') expect(empty.slot.height).toBeLessThanOrEqual(empty.slotLineHeight);
        for (const [index, message] of messages.entries()) {
          await page.locator('#feedback').evaluate((node, value) => { node.textContent = value; }, message);
          const filled = await layout();
          if (surface === 'setup') {
            expect.soft(filled.action, `message ${index}`).toEqual(empty.action);
            expect.soft(filled.following[1], `message ${index}`).toBeGreaterThanOrEqual(empty.following[1]);
            expect.soft(filled.following[1], `message ${index}`).toBeGreaterThanOrEqual(filled.slot.bottom);
          } else {
            expect.soft(filled.following, `message ${index}`).toEqual(empty.following);
            expect.soft(filled.slot.height, `message ${index}`).toBe(empty.slot.height);
          }
          expect.soft(filled.feedback.bottom, `message ${index}`).toBeLessThanOrEqual(filled.slot.bottom + 1);
          await page.locator('#feedback').evaluate((node) => { node.textContent = ''; });
          expect.soft((await layout()).following, `cleared ${index}`).toEqual(empty.following);
        }
      });
    }

    test(`options controls use page scrolling at ${width}px and ${scale * 100}% text`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await renderSurfaceState(page, SURFACE_STATES.connection);
      await page.evaluate((factor) => {
        document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
      }, scale);
      const frames = page.locator('.options-frame');
      const frameLayout = await frames.evaluateAll((elements) => elements.map((frame) => ({
        overflow: getComputedStyle(frame).overflowY,
        excess: frame.scrollHeight - frame.clientHeight,
      })));
      expect(frameLayout, `${width}px at ${scale * 100}%`).toEqual([
        { overflow: 'auto', excess: 0 },
        { overflow: 'auto', excess: 0 },
        { overflow: 'auto', excess: 0 },
      ]);
      const unfilled = await frames.evaluateAll((elements) => elements.map((frame) =>
        frame.getBoundingClientRect().height - parseFloat(getComputedStyle(frame).paddingBottom)
          - frame.querySelector(':scope > section').getBoundingClientRect().height));
      expect(unfilled.every((gap) => Math.abs(gap) < 1), `${width}px at ${scale * 100}%: ${unfilled}`).toBe(true);
      const controls = frames.locator('button:visible');
      expect(await controls.count()).toBeGreaterThan(0);
      for (const control of await controls.all()) {
        await control.scrollIntoViewIfNeeded();
        const bounds = await control.boundingBox();
        expect(bounds.y, await control.getAttribute('id')).toBeGreaterThanOrEqual(0);
        expect(bounds.y + bounds.height, await control.getAttribute('id')).toBeLessThanOrEqual(641);
      }
      expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
      const frameTops = () => frames.evaluateAll((elements) => elements.map((frame) => frame.getBoundingClientRect().top + window.scrollY));
      const before = await frameTops();
      await page.evaluate(() => window.__surfaceFixture.change({ phase: 'permission_required' }));
      const restoreAccess = page.locator('#restore-access');
      await expect(restoreAccess).toBeVisible();
      expect(await frameTops()).toEqual(before);
      await restoreAccess.scrollIntoViewIfNeeded();
      const restoreBounds = await restoreAccess.boundingBox();
      expect(restoreBounds.y).toBeGreaterThanOrEqual(0);
      expect(restoreBounds.y + restoreBounds.height).toBeLessThanOrEqual(641);
    });

    for (const surface of ['popup', 'setup', 'options']) {
      test(`${surface} feedback stays clear of the following content at ${width}px and ${scale * 100}% text`, async ({ page }) => {
        await page.setViewportSize({ width, height: 640 });
        const state = surface === 'popup' ? SURFACE_STATES.preview
          : surface === 'setup' ? SURFACE_STATES.activation_required : SURFACE_STATES.manage;
        await renderSurfaceState(page, state);
        await page.evaluate((factor) => {
          document.documentElement.style.fontSize = `${parseFloat(getComputedStyle(document.documentElement).fontSize) * factor}px`;
        }, scale);
        const layout = () => page.evaluate(() => {
          const status = document.querySelector('.reserved-status');
          const feedback = status.querySelector('.feedback');
          const following = document.querySelector('body[data-surface="popup"] nav.menu')
            || document.querySelector('body[data-surface="options"] footer')
            || document.querySelector('body[data-surface="setup"] footer');
          return {
            status: status.getBoundingClientRect().toJSON(),
            feedback: feedback.getBoundingClientRect().toJSON(),
            following: following.getBoundingClientRect().toJSON(),
            lineHeight: parseFloat(getComputedStyle(feedback).lineHeight),
            fontSize: parseFloat(getComputedStyle(feedback).fontSize),
          };
        });
        const empty = await layout();
        await page.locator('#feedback').evaluate((element) => { element.innerHTML = 'Status ready.<br>Try again.'; });
        const filled = await layout();
        expect(filled.lineHeight).toBeGreaterThanOrEqual(filled.fontSize * 1.4);
        expect(filled.feedback.height).toBeGreaterThanOrEqual(filled.lineHeight * 2 - 1);
        expect(filled.status.bottom).toBeGreaterThanOrEqual(filled.feedback.bottom - 1);
        expect(filled.following.top).toBeGreaterThanOrEqual(filled.feedback.bottom - 1);
        if (surface === 'setup') expect(filled.status.top).toBe(empty.status.top);
        else expect(filled.following.top).toBeCloseTo(empty.following.top, 0);
      });
    }
  }
}

for (const width of [1280, 390, 320]) {
  test(`options notice band holds the loading line at ${width}px and the frames keep their place`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const rendering = renderSurfaceState(page, { ...SURFACE_STATES.manage, initialRequestHold: 'ofca.ui.status' });
    await page.waitForFunction(() => typeof window.__releaseInitialRequest === 'function');
    await expect(page.locator('#loading')).toBeVisible();
    const band = await page.locator('.reserved-notice').boundingBox();
    const loading = await page.locator('#loading').boundingBox();
    expect(Math.abs(loading.y - band.y)).toBeLessThan(0.5);
    expect(Math.abs(loading.height - band.height)).toBeLessThan(0.5);
    const frameTops = () => page.locator('.options-frame').evaluateAll((frames) => frames.map((frame) => frame.getBoundingClientRect().top));
    const before = await frameTops();
    await page.evaluate(() => window.__releaseInitialRequest());
    await rendering;
    await expect(page.locator('#loading')).toBeHidden();
    expect(await frameTops()).toEqual(before);
  });
}

test('only the admitted task presents Full consent and permission actions', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.software_activation, browserTask: true });
  await expect(page.locator('#setup-progress')).toHaveCount(0);
  await expect(page.locator('#pre-mode')).toBeVisible();
  await page.locator('#terms-accepted').check();
  await page.locator('#risk-acknowledged').check();
  await page.locator('#enable-full').click();
  await expect(page.locator('#journey-title')).toHaveText('Browser access is ready');
  await expect(page.locator('#pairing-code')).toHaveCount(0);
  expect((await calls(page)).some(call => call.type === 'pair')).toBe(false);
});

test('activation changes leave Full continuation in the desktop app', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_required);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await page.evaluate(() => window.__surfaceFixture.change({ commercial: 'active', ready: true }));
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(page.locator('#open-desktop')).toBeEnabled();
  await expect(page.locator('#setup-progress, #pairing-code')).toHaveCount(0);
  expect((await calls(page)).some(call => call.type === 'retry_readiness')).toBe(false);
});

test('blocked analysis offers the desktop app without a competing repair workflow', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.activation_active);
  await expect(page.locator('#desktop-entry')).toBeVisible();
  await expect(page.locator('#open-desktop')).toBeEnabled();
  await expect(page.locator('#journey-card')).toBeHidden();
});

test('a failed popup command reports the failure without a generic retry instruction', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await page.evaluate(() => {
    const send = chrome.runtime.sendMessage;
    chrome.runtime.sendMessage = async (message) => message.type === 'ofca.ui.open-surface'
      ? { ok: false } : send(message);
  });
  await page.locator('#journey-primary').click();
  await expect(page.locator('#feedback')).toHaveText('That did not complete.');
});

test('unavailable popup activation keeps its setup route and never offers a retry', async ({ page }) => {
  await renderSurfaceState(page, { ...SURFACE_STATES.full_ready, commercial: 'unavailable', ready: false });
  await expect(page.locator('#journey-title')).toHaveText('Needs attention');
  await expect(page.locator('#journey-body')).toHaveText('Activation could not be checked.');
  await expect(page.getByRole('button', { name: 'Check again', exact: true })).toHaveCount(0);
  await expect(page.locator('#journey-primary')).toHaveText('Continue in the desktop app');
  await page.locator('#journey-primary').click();
  expect((await calls(page)).filter(call => call.type === 'ofca.ui.open-surface')).toEqual([
    { type: 'ofca.ui.open-surface', surface: 'setup', section: 'full' },
  ]);
});

test('popup readiness follows connection loss without leaving a stale ready claim', async ({ page }) => {
  await renderSurfaceState(page, SURFACE_STATES.full_ready);
  await expect(page.locator('#ready-details')).toBeVisible();
  await expect(page.locator('#desktop-status')).toHaveText('Connected');
  await expect(page.locator('#delivery-status')).toHaveText('Connected');
  await expect(page.locator('#activation-status')).toHaveText('Active');
  await expect(page.locator('#analysis-status')).toHaveText('Ready');
  await page.evaluate(() => {
    window.__surfaceFixture.change({ reachable: false });
    window.dispatchEvent(new Event('focus'));
  });
  await expect(page.locator('#desktop-status')).toHaveText('Not connected');
  await expect(page.locator('#delivery-status')).toHaveText('Not connected');
  await expect(page.locator('#analysis-status')).toHaveText('Not ready');
});
