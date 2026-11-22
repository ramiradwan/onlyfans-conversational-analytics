import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { reviewCases } from './ci/inventory.mjs';

export function contrastAgainst(foreground, backdrop) {
  const rgb = foreground.slice(0, 3).map((c, i) => (c * foreground[3] + backdrop[i] * (1 - foreground[3])) / 255);
  const luminance = (channels) => channels.map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
    .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
  const a = luminance(rgb), b = luminance(backdrop.slice(0, 3).map((c) => c / 255));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
export function assertStateHierarchy(rest, hovered, selected, paper) {
  for (const property of ['fill', 'ink']) {
    const ranks = [rest, hovered, selected].map((state) => contrastAgainst(state[property], paper));
    assert(ranks[2] > ranks[1] && ranks[1] > ranks[0], `${property}: selected > hover > rest failed: ${ranks}`);
  }
}
export const near = (value, expected, label, tolerance = 0.6) => assert(Math.abs(value - expected) <= tolerance, `${label}: ${value}, expected ${expected}`);
async function appearance(locator) {
  return locator.evaluate((element) => {
    const css = getComputedStyle(element), r = element.getBoundingClientRect();
    const rgba = (color) => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
      const context = canvas.getContext('2d'); context.fillStyle = color; context.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data; return [r, g, b, a / 255];
    };
    return { x: r.x, y: r.y, width: r.width, height: r.height, canvas: rgba(css.getPropertyValue('--bridge-palette-background-default') || getComputedStyle(document.body).backgroundColor), fill: rgba(css.backgroundColor), ink: rgba(css.color),
      background: css.backgroundColor, gradient: css.backgroundImage, filter: css.backdropFilter, radius: css.borderRadius, font: css.fontFamily,
      fontSize: parseFloat(css.fontSize), spacing: css.letterSpacing, border: css.borderWidth, animation: css.animationName,
      duration: css.animationDuration, delay: css.animationDelay, iterations: css.animationIterationCount,
      outlineWidth: css.outlineWidth, outlineStyle: css.outlineStyle, outlineOffset: css.outlineOffset, focusVisible: element.matches(':focus-visible') };
  });
}
async function centerPixel(page, locator) {
  // Isolate the shared background from the two SVG renderers for the pixel comparison.
  const old = await locator.locator('svg').evaluateAll((nodes) => nodes.map((n) => { const value = n.style.visibility; n.style.visibility = 'hidden'; return value; }));
  const png = await locator.screenshot({ animations: 'disabled' });
  await locator.locator('svg').evaluateAll((nodes, values) => nodes.forEach((n, i) => { n.style.visibility = values[i]; }), old);
  return page.evaluate(async (encoded) => {
    const image = new Image(); image.src = 'data:image/png;base64,' + encoded; await image.decode();
    const c = document.createElement('canvas'); c.width = image.width; c.height = image.height;
    const ctx = c.getContext('2d'); ctx.drawImage(image, 0, 0);
    return [...ctx.getImageData(Math.floor(c.width / 2), Math.floor(c.height / 2), 1, 1).data];
  }, png.toString('base64'));
}
export function assertTrustAnchors(before, after) {
  for (const [name, box] of Object.entries(before)) {
    near(after[name].x, box.x, name + ' x');
    near(after[name].y, box.y, name + ' y');
  }
}

export async function measureTrustPasskeys(page, open, mode, screenshot) {
  const measurements = [];
  const targets = [
    { width: 1440, height: 900, scale: 1 },
    { width: 390, height: 844, scale: 1 },
    { width: 320, height: 900, scale: 2 },
  ];
  for (const { width, height, scale } of targets) {
    await page.setViewportSize({ width, height });
    await open('home');
    const homeBrand = await appearance(page.locator('[data-visual="brand-tile"]').first());
    await open('passkey', 'resting&transitions=1');
    if (scale === 2) await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    const main = page.getByRole('main');
    const card = main.locator('[data-composition="trust"]');
    const brand = page.locator('[data-visual="brand-tile"]').filter({ visible: true });
    const heading = card.getByRole('heading', { level: 1, name: 'Confirm it’s you' });
    const tile = card.locator('[data-visual="passkey-lock"] .onboarding-spot');
    const signIn = card.getByRole('button', { name: 'Sign in with passkey', exact: true });
    const setup = card.getByRole('button', { name: 'Set up a passkey', exact: true });
    const footer = page.getByRole('contentinfo');
    const banner = page.getByRole('banner');
    const feedback = card.locator('[data-visual="passkey-feedback"]');
    await heading.waitFor();
    assert.equal(await brand.count(), 1);
    assert.equal(await main.getByRole('banner').count(), 0);
    assert.equal(await card.locator('.MuiButton-contained').count(), 1);
    assert(await signIn.evaluate(node => node.classList.contains('MuiButton-contained')));
    assert.equal(await card.locator('[data-visual="brand-tile"]').count(), 0);
    const [brandBox, headerBox, cardBox, tileBox, titleBox, mainBox] = await Promise.all(
      [brand, banner, card, tile, heading, main].map(appearance),
    );
    near(brandBox.width, 32, 'passkey brand width');
    near(brandBox.height, 32, 'passkey brand height');
    assert(headerBox.y + headerBox.height <= cardBox.y);
    assert(brandBox.x >= headerBox.x && brandBox.y >= headerBox.y
      && brandBox.x + brandBox.width <= headerBox.x + headerBox.width
      && brandBox.y + brandBox.height <= headerBox.y + headerBox.height, 'The brand must fit the header');
    assert(cardBox.x >= mainBox.x && cardBox.x + cardBox.width <= mainBox.x + mainBox.width);
    near(tileBox.width, 40, 'passkey spot width');
    near(tileBox.height, 40, 'passkey spot height');
    assert.equal(tileBox.radius, '12px');
    assert(tileBox.x > cardBox.x && tileBox.y > cardBox.y && tileBox.y + tileBox.height <= titleBox.y);
    assert(contrastAgainst(tileBox.ink, tileBox.fill) >= 3, 'passkey spot contrast');
    near(titleBox.fontSize, width > 600 ? 24 : 22 * scale, 'passkey title');
    assert(titleBox.font.includes('Inter'));
    near((await appearance(feedback)).height, 0, 'empty feedback height');
    for (let index = 0; index < 4 && !await setup.evaluate(node => document.activeElement === node); index++) {
      await page.keyboard.press('Tab');
    }
    await page.waitForFunction(node => node.matches(':focus-visible') && getComputedStyle(node).outlineWidth === '2px',
      await setup.elementHandle(), { timeout: 3000 });
    near(parseFloat((await appearance(setup)).outlineWidth), 2, 'setup keyboard focus ring');
    if (scale === 1) await screenshot(page, `passkey-layout-${mode}-${width}.png`);

    const anchors = async () => {
      const scrollTop = await page.evaluate(() => window.scrollY);
      const fixed = { brand, banner, footer };
      const inCard = { heading, tile, action: card.locator('.onboarding-action-content'), body: card.locator('.onboarding-surface-body') };
      return Object.fromEntries(await Promise.all(Object.entries({ ...fixed, ...inCard }).map(async ([name, locator]) => {
        const box = await appearance(locator);
        box.y += scrollTop;
        return [name, box];
      })));
    };
    const rest = await anchors();
    await signIn.click();
    const pending = card.getByRole('status').filter({ hasText: 'Waiting for your passkey…' });
    await pending.waitFor();
    const pendingBoxes = await anchors();
    assertTrustAnchors(rest, pendingBoxes);
    near(pendingBoxes.action.height, rest.action.height, 'pending action height');
    near((await appearance(pending)).height, rest.action.height, 'pending status row height');
    assert.equal(await signIn.count(), 0);
    assert.equal((await appearance(pending.locator('svg'))).animation, 'none', 'reduced-motion pending arc');
    assert.equal((await appearance(card.locator('[data-swap]'))).animation, 'none', 'reduced-motion action swap');
    await page.evaluate(() => window.__workspaceFixture.reject('passkey.login', 'NotAllowedError'));
    const summary = feedback.getByRole('alert');
    await summary.waitFor();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const failed = await anchors();
    const { footer: restingFooter, ...restContent } = rest;
    assertTrustAnchors(restContent, failed);
    near(failed.action.height, rest.action.height, 'restored action height');
    const feedbackBox = await appearance(feedback);
    const summaryStyle = await appearance(summary);
    const failedCard = await appearance(card);
    const liveAction = await appearance(card.locator('.onboarding-action-content'));
    assert(feedbackBox.y >= liveAction.y + liveAction.height && feedbackBox.y + feedbackBox.height < failedCard.y + failedCard.height);
    const flow = await main.evaluate(node => ({
      cardBottom: node.querySelector('[data-composition="trust"]').getBoundingClientRect().bottom + window.scrollY,
      footerTop: document.querySelector('footer').getBoundingClientRect().top + window.scrollY,
      padding: parseFloat(getComputedStyle(node.parentElement).paddingBottom),
    }));
    near(failed.footer.x, restingFooter.x, 'footer x');
    near(flow.footerTop, Math.max(restingFooter.y, flow.cardBottom + flow.padding), 'footer follows card');
    near(failed.footer.height, restingFooter.height, 'footer height');
    assert(contrastAgainst(summaryStyle.ink, cardBox.fill) >= 4.5, 'passkey error text contrast');
    assert.equal(await summary.innerText(), 'Sign-in didn’t finish.');
    assert.equal((await appearance(summary.locator('[data-arrive]'))).animation, 'none', 'reduced-motion status end state');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await summary.scrollIntoViewIfNeeded();
    const visibleFeedback = await appearance(summary);
    assert(visibleFeedback.y >= 0 && visibleFeedback.y + visibleFeedback.height <= height
      && visibleFeedback.y + visibleFeedback.height <= (await appearance(footer)).y, 'Passkey feedback can scroll into view above the footer');
    if (width === 1440) {
      for (const [locator, limit] of [[heading, 1], [card.locator('.onboarding-surface-body'), 2], [summary.locator('.onboarding-status span'), 1]]) {
        assert(await locator.evaluate((node, max) => {
          const range = document.createRange();
          range.selectNodeContents(node);
          return new Set([...range.getClientRects()].map(rect => rect.top)).size <= max;
        }, limit), 'passkey desktop copy budget');
      }
      await footer.evaluate(node => {
        node.style.position = 'relative';
        node.style.insetBlockStart = '2px';
      });
      try {
        const displaced = await anchors();
        assert.throws(() => assertTrustAnchors(failed, displaced), /footer y/);
      } finally {
        await footer.evaluate(node => {
          node.style.removeProperty('position');
          node.style.removeProperty('inset-block-start');
        });
      }
    }
    if (scale === 1) await screenshot(page, `passkey-error-${mode}-${width}.png`);
    measurements.push({ width, height, scale, homeBrand, cardBox, tileBox, titleBox, rest, pendingBoxes, failed, feedbackBox, summaryStyle });
  }
  return measurements;
}

async function textLines(locator) {
  return locator.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const text = [...range.getClientRects()];
    const bounds = element.getBoundingClientRect();
    return {
      count: new Set(text.map(rect => rect.top)).size,
      fits: text.every(rect => rect.left >= bounds.left - 0.5 && rect.right <= bounds.right + 0.5
        && rect.top >= bounds.top - 0.5 && rect.bottom <= bounds.bottom + 0.5),
    };
  });
}

export async function measureDashboardOnboarding(page, open) {
  const layouts = [];
  for (const [width, scale] of [[1440, 1], [390, 1], [320, 2]]) {
    await page.setViewportSize({ width, height: 900 });
    const dashboard = page.getByRole('heading', { name: 'Dashboard', level: 1, exact: true });
    await open('home', 'fresh');
    await page.evaluate(value => { document.documentElement.style.fontSize = `${value * 100}%`; }, scale);
    const setup = page.locator('[data-visual="setup-prompt"]');
    const title = setup.getByRole('heading', { name: 'Set up your browser to see your message counts', exact: true });
    await title.waitFor();
    near((await appearance(setup)).x, (await appearance(dashboard)).x, 'Setup card left edge');
    const titleLines = await textLines(title);
    const bodyLines = await textLines(setup.locator('.onboarding-surface-body'));
    assert(titleLines.fits && bodyLines.fits, 'Dashboard setup copy clips');
    if (width === 1440) {
      assert.equal(titleLines.count, 1);
      assert(bodyLines.count <= 2);
    } else if (scale === 2) assert(titleLines.count > 1 && bodyLines.count > 2, 'Narrow dashboard setup copy must wrap');
    assert.equal(await setup.getByRole('link', { name: 'Continue in browser', exact: true }).getAttribute('href'), '/settings#browser-extension');

    await open('home');
    await page.evaluate(value => { document.documentElement.style.fontSize = `${value * 100}%`; }, scale);
    await page.evaluate(() => {
      const fixture = window.__workspaceFixture;
      const snapshot = fixture.sample();
      const { inbound_messages, outbound_messages } = snapshot.analytics;
      inbound_messages.sample_size = outbound_messages.sample_size = inbound_messages.value + outbound_messages.value;
      fixture.delta({ creator_account_id: snapshot.creator_account_id, view_revision: snapshot.view_revision + 1,
        committed_at: snapshot.generated_at, changes: [{ type: 'analytics.replace', analytics: snapshot.analytics }] });
    });
    const basis = page.locator('[data-dashboard-saved-basis]');
    await basis.waitFor();
    assert.match(await basis.innerText(), /^Based on [\d,]+ messages saved on this computer so far\.$/u);
    const sent = page.locator('[data-reserved-region="metric-Sent"]');
    assert((await sent.boundingBox()).y < (await basis.boundingBox()).y, 'The saved basis must follow the counts');
    near((await appearance(basis)).x, (await appearance(dashboard)).x, 'Saved basis left edge');
    const count = page.locator('[data-visual="message-total"] [data-region-content]');
    assert.equal((await appearance(count)).animation, 'none');

    const connectionPill = page.getByRole('region', { name: 'Connection', exact: true })
      .locator('.onboarding-pill').filter({ hasText: /^Connected$/u }).first();
    const pillStyle = await appearance(connectionPill);
    const dotStyle = await appearance(connectionPill.locator('.onboarding-tone-dot'));
    near(dotStyle.width, 6, 'Connection status dot');
    assert.equal(await connectionPill.getAttribute('data-tone'), 'progress');
    assert.equal(pillStyle.fontSize, 12 * scale);

    const details = page.getByRole('button', { name: 'Details', exact: true });
    await page.keyboard.press('Tab');
    await details.focus();
    assert((await appearance(details)).focusVisible);
    await page.keyboard.press('Enter');
    await page.getByRole('dialog', { name: 'How these numbers are counted' }).waitFor();
    await page.keyboard.press('Escape');
    await page.waitForFunction(node => document.activeElement === node, await details.elementHandle());

    const overview = page.getByRole('region', { name: 'Overview', exact: true });
    const beforePause = await appearance(overview);
    await page.evaluate(() => window.__workspaceFixture.browser({ capture: 'paused' }));
    const pause = page.getByRole('region', { name: 'Paused by you', exact: true });
    await pause.waitFor();
    near((await appearance(overview)).y, beforePause.y, 'Overview y after pushed pause');
    near((await appearance(pause)).x, beforePause.x, 'Pause card left edge');
    assert((await appearance(pause)).width <= 760, 'Pause card exceeds its guided width');
    assert.match(await basis.innerText(), / Last updated .+\.$/u);
    assert.equal(await page.getByRole('button', { name: 'Review in browser', exact: true }).count(), 1);
    const basisLines = await textLines(basis);
    assert(basisLines.fits);
    if (width === 1440) assert.equal(basisLines.count, 1);

    if (scale === 2) {
      await page.evaluate(() => {
        const fixture = window.__workspaceFixture;
        const snapshot = fixture.sample();
        snapshot.analytics.total_conversations.value = 1_234_567;
        snapshot.analytics.total_messages.value = 9_999_999;
        fixture.delta({ creator_account_id: snapshot.creator_account_id, view_revision: fixture.state().viewRevision + 1,
          committed_at: snapshot.generated_at, changes: [{ type: 'analytics.replace', analytics: snapshot.analytics }] });
      });
      for (const [name, value] of [['conversation-total', '1,234,567'], ['message-total', '9,999,999']]) {
        const numeral = page.locator(`[data-visual="${name}"]`);
        await numeral.getByText(value, { exact: true }).waitFor();
        assert((await textLines(numeral)).fits, `${value} escapes its count row`);
      }
    } else {
      const status = page.locator('[data-reserved-region="dashboard-notice"] .onboarding-status');
      const verifyStatus = async (text, tone) => {
        const label = status.getByText(text, { exact: true });
        await label.waitFor();
        assert.equal(await status.getAttribute('data-tone'), tone);
        const mark = await appearance(status.locator('svg'));
        near(mark.width, 20, 'Freshness icon width');
        near(mark.height, 20, 'Freshness icon height');
        const labelBox = await appearance(label);
        assert.deepEqual(labelBox.ink, (await appearance(dashboard)).ink);
        assert((await basis.boundingBox()).y < labelBox.y, 'Freshness must follow the count basis');
        assert(labelBox.y + labelBox.height <= 900, 'Freshness must be above the fold');
        if (width === 1440) assert.equal((await textLines(label)).count, 1);
        if (tone === 'pending') assert.equal(mark.animation, 'none');
      };
      await page.evaluate(() => { window.__workspaceFixture.disconnected(); window.__workspaceFixture.connection('reconnecting'); });
      await verifyStatus('Reconnecting to the app…', 'pending');
      await page.evaluate(() => window.__workspaceFixture.connection('disconnected'));
      await verifyStatus('Dipsy can’t connect right now. Showing your saved counts.', 'attention');
      await page.evaluate(() => window.__workspaceFixture.connection('connected'));
      await verifyStatus('Couldn’t check the latest update. Showing your saved counts.', 'attention');
      await page.evaluate(() => window.__workspaceFixture.browser({ history_error_code: 'software_update_required' }));
      await verifyStatus('Update Conversation Analytics to continue.', 'attention');
    }
    layouts.push({ width, scale, titleLines, bodyLines, basisLines });
  }

  await page.emulateMedia({ reducedMotion: 'no-preference' });
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await open('home', 'loading');
    const overview = page.getByRole('region', { name: 'Overview', exact: true });
    await page.getByRole('region', { name: 'Overview', busy: true }).waitFor();
    const loadingOverview = await appearance(overview);
    await page.evaluate(() => window.__workspaceFixture.snapshot('populated'));
    await page.getByRole('region', { name: 'Overview', busy: false }).waitFor();
    const count = page.locator('[data-visual="message-total"] [data-region-content]');
    const motion = await appearance(count);
    assert.notEqual(motion.animation, 'none');
    assert.equal(motion.duration, '0.2s');
    assert.equal(motion.iterations, '1');
    for (const label of ['Received', 'Sent']) {
      const numeral = page.locator(`[data-reserved-region="metric-${label}"] [data-region-content]`);
      const style = await appearance(numeral);
      assert.equal(style.duration, '0.2s');
      assert(parseFloat(style.delay) <= 0.08, `${label} arrives too late`);
    }
    const animationStart = () => count.evaluate(async node => {
      const [animation] = node.getAnimations();
      await animation.ready;
      return animation.startTime;
    });
    const start = await animationStart();
    await page.evaluate(() => {
      const fixture = window.__workspaceFixture;
      const snapshot = fixture.sample();
      snapshot.analytics.total_messages.value += 1;
      fixture.delta({ creator_account_id: snapshot.creator_account_id, view_revision: snapshot.view_revision + 1,
        committed_at: snapshot.generated_at, changes: [{ type: 'analytics.replace', analytics: snapshot.analytics }] });
    });
    assert.equal(await animationStart(), start, 'An updated count must not replay its arrival');
    await page.evaluate(() => {
      const fixture = window.__workspaceFixture;
      fixture.connection('connected');
      fixture.agent(fixture.agentSample());
      fixture.resolve('onboarding.readiness', { schema: 'ofca-analysis-readiness/v1',
        commercial_authority: 'active', analysis_admission: 'admitted' });
    });
    await page.getByRole('region', { name: 'Connection', exact: true }).waitFor();
    near((await appearance(overview)).y, loadingOverview.y, 'Overview y after snapshot and readiness');
    return { layouts, motion: { duration: motion.duration, delay: motion.delay, iterations: motion.iterations } };
  } finally {
    await page.emulateMedia({ reducedMotion: 'reduce' });
  }
}

/** Separate interaction probes; they never alter a standard capture's state. */
export async function captureReviewChecks(browser, base, outDir, recorder = null) {
  const directory = join(outDir, 'review'); await mkdir(directory, { recursive: true });
  const checks = [], failures = [];
  let capturedFiles = [];
  const screenshot = async (page, filename) => {
    await page.screenshot({ path: join(directory, filename), animations: 'disabled' });
    capturedFiles.push('review/' + filename);
  };
  const record = async (name, action) => {
    const definition = reviewCases().find(value => value.name === name);
    const finish = recorder?.begin(`review-${definition?.mode}-${definition?.key}`, { mode: definition?.mode, name });
    capturedFiles = ['review/acceptance.json'];
    let passed = false;
    try { checks.push({ name, result: 'passed', measurements: await action() }); passed = true; }
    catch (error) { failures.push(`${name}: ${error.message}`); }
    finally { finish?.({ outcome: passed ? 'passed' : 'failed', observations: passed ? [name] : [], files: capturedFiles }); }
  };
  const popupHtml = (await readFile(new URL('../../extension/popup.html', import.meta.url), 'utf8'))
    .replace('<link rel="stylesheet" href="popup.css">', `<style>${await readFile(new URL('../../extension/popup.css', import.meta.url), 'utf8')}</style>`)
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
  for (const mode of ['light', 'dark']) {
    const context = await browser.newContext({ colorScheme: mode, locale: 'en-US', reducedMotion: 'reduce', timezoneId: 'UTC', viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    await page.clock.setFixedTime('2026-06-30T12:05:00.000Z');
    const open = async (workspace, state = 'populated') => {
      await page.goto(`${base}?workspace=${workspace}&state=${state}&mode=${mode}`, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
    };
    try {
      await record(`${mode}: rail geometry, state hierarchy, tooltip and focus`, async () => {
        await open('home');
        const rail = page.getByLabel('Desktop navigation', { exact: true });
        const paper = await appearance(rail), canvas = { fill: paper.canvas };
        const links = rail.getByRole('link'); const boxes = [];
        for (const link of await links.all()) {
          const box = await appearance(link); boxes.push(box);
          near(box.width, 48, 'rail item width'); near(box.height, 44, 'rail item height');
          near(box.x - paper.x, 8, 'left inset'); near(paper.x + paper.width - box.x - box.width, 8, 'right inset');
        }
        const header = await appearance(page.getByRole('banner'));
        const heading = await appearance(page.locator('main h1').first());
        const railInset = JSON.parse(await readFile(new URL('../../frontend/src/theme/tokens.json', import.meta.url), 'utf8')).tier3.shell.railInset;
        near(paper.y - header.y - header.height, railInset, 'rail gap below header');
        near(page.viewportSize().height - paper.y - paper.height, railInset, 'rail bottom inset');
        near(heading.y - header.y - header.height, railInset, 'heading gap below header');
        near(boxes[0].y - paper.y, 8, 'top inset'); assert.equal(paper.filter, 'none'); assert.equal(paper.fill[3], 1);
        if (mode === 'dark') assert(paper.fill.slice(0, 3).every((channel, i) => channel > canvas.fill[i]), 'rail must be lighter than canvas');
        const inbox = rail.getByRole('link', { name: 'Inbox', exact: true }); const rest = await appearance(inbox);
        const selected = await appearance(links.first()); await inbox.hover();
        await page.locator('.MuiTooltip-tooltip').waitFor({ state: 'visible' });
        const hovered = await appearance(inbox), tooltip = await appearance(page.locator('.MuiTooltip-tooltip'));
        assertStateHierarchy(rest, hovered, selected, paper.fill); near(tooltip.fontSize, 12, 'tooltip size');
        assert(contrastAgainst(tooltip.ink, [...tooltip.fill.slice(0, 3).map((c, i) => c * tooltip.fill[3] + paper.fill[i] * (1 - tooltip.fill[3])), 1]) >= 4.5, 'tooltip text contrast');
        await screenshot(page, `rail-hover-${mode}.png`);
        await page.mouse.move(400, 400); await page.keyboard.press('Tab'); await inbox.focus();
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const focus = await appearance(inbox); assert.deepEqual(focus.fill, hovered.fill); assert.notEqual(focus.background, 'rgba(0, 0, 0, 0.12)');
        assert(focus.focusVisible, 'keyboard focus is missing');
        assert.equal(focus.outlineWidth, '2px'); assert.equal(focus.outlineStyle, 'solid'); assert.equal(focus.outlineOffset, '-2px');
        await screenshot(page, `rail-focus-${mode}.png`);
        return { paper, canvas, header, heading, railInset, boxes, rest, hovered, selected, focus, tooltip };
      });
      await record(`${mode}: popup and app brand pixels`, async () => {
        await open('home');
        const app = page.locator('[data-visual="brand-tile"]').first();
        const appPixel = await centerPixel(page, app), appStyle = await appearance(app);
        const popup = await context.newPage();
        try {
          await popup.setContent(popupHtml);
          const tile = popup.locator('.brand-mark'); const popupStyle = await appearance(tile);
          const popupPixel = await centerPixel(popup, tile);
          assert.equal(popupStyle.gradient, appStyle.gradient); assert.deepEqual(popupStyle.ink, appStyle.ink);
          // The popup tile sits on a fractional CSS pixel; a gradient sample can round by one channel level.
          assert(popupPixel.every((channel, i) => Math.abs(channel - appPixel[i]) <= 1), 'brand center differs beyond raster rounding');
          return { appPixel, popupPixel, appStyle, popupStyle };
        } finally { await popup.close(); }
      });
      await record(`${mode}: narrow navigation and restored keyboard focus`, async () => {
        await page.setViewportSize({ width: 390, height: 844 }); await open('home');
        const menu = page.getByRole('button', { name: 'Open navigation' });
        const box = await appearance(menu); assert(box.x >= 0, 'menu button clips the viewport');
        await page.keyboard.press('Tab'); await menu.focus(); await page.keyboard.press('Enter');
        const drawer = page.getByLabel('Mobile navigation', { exact: true });
        await drawer.waitFor({ state: 'visible' }); const drawerStyle = await appearance(drawer);
        assert.equal(drawerStyle.radius, '0px 20px 20px 0px');
        await screenshot(page, `mobile-drawer-${mode}.png`);
        await page.waitForFunction(() => document.querySelector('#mobile-navigation').contains(document.activeElement));
        await page.keyboard.press('Escape'); await drawer.waitFor({ state: 'hidden' });
        assert(await menu.evaluate((node) => document.activeElement === node), 'Escape lost menu focus');
        const focus = await appearance(menu); assert.notEqual(focus.background, 'rgba(0, 0, 0, 0.12)');
        assert(focus.focusVisible, 'restored menu focus is not visible'); assert.equal(focus.outlineWidth, '2px');
        assert.equal(await menu.locator('.MuiTouchRipple-childPulsate').count(), 0, 'default keyboard ripple obscures the authored focus fill');
        await screenshot(page, `mobile-focus-${mode}.png`);
        return { box, drawer: drawerStyle, focus };
      });
      for (const width of [1440, 820, 390]) {
        await record(`${mode}: analytics typography and tracks at ${width}px`, async () => {
          await page.setViewportSize({ width, height: 900 }); await open('analytics', 'model');
          const numeral = await appearance(page.locator('.MuiTypography-insight'));
          near(numeral.fontSize, 46, 'latest tone size'); near(parseFloat(numeral.spacing), -1.38, 'latest tone tracking');
          const units = await page.locator('[data-metric-unit]').evaluateAll((nodes) => nodes.map((n) => ({ text: n.textContent, size: parseFloat(getComputedStyle(n).fontSize) })));
          assert(units.some((unit) => unit.text === 'min')); assert(units.some((unit) => unit.text === '%'));
          units.forEach((unit) => near(unit.size, 20, 'unit size'));
          const coverage = await appearance(page.locator('[data-visual="reply-coverage-track"]'));
          const fill = await appearance(page.locator('[data-visual="reply-coverage-fill"]'));
          near(coverage.height, 8, 'coverage height'); near(fill.width / coverage.width, 0.75, 'coverage ratio', 0.005);
          const tracks = [];
          for (const track of await page.locator('[data-visual="topic-track"]:visible').all()) tracks.push(await appearance(track));
          assert.equal(tracks.length, 8);
          for (const track of tracks) { near(track.x, tracks[0].x, 'topic track start'); near(track.x + track.width, tracks[0].x + tracks[0].width, 'topic track end'); near(track.height, 7, 'topic track height'); }
          const toggle = await appearance(page.getByRole('button', { name: 'Chart', exact: true })); near(toggle.fontSize, 12, 'toggle size');
          const baseline = page.locator('[data-visual="tone-baseline"]'); assert.equal(await baseline.getAttribute('stroke-dasharray'), '4 4');
          assert.equal(await baseline.getAttribute('stroke-width'), '1');
          const endpoint = page.locator('[data-endpoint="true"]'); assert.equal(await endpoint.getAttribute('data-hit-target'), '24');
          assert.equal(await endpoint.locator('.chart-marker-symbol').textContent(), '+');
          await endpoint.focus(); await page.getByRole('tooltip').waitFor({ state: 'visible' }); assert(await page.getByRole('tooltip').isVisible(), 'endpoint has no keyboard tooltip');
          await page.keyboard.press('Escape');
          await page.getByRole('button', { name: 'Table', exact: true }).click();
          assert(await page.getByRole('table', { name: 'Message tone over time data' }).isVisible());
          return { numeral, units, coverage, fill, tracks, toggle };
        });
      }
      await record(`${mode}: recent rows, status and passkey`, async () => {
        await page.setViewportSize({ width: 390, height: 844 }); await open('home');
        const avatar = await appearance(page.locator('.MuiAvatar-root').first()); near(avatar.width, 34, 'avatar width');
        assert.equal(avatar.radius, '11px'); near(avatar.fontSize, 13, 'avatar label');
        const timestamp = await appearance(page.locator('time').first()); assert(timestamp.font.includes('Space Grotesk')); near(timestamp.fontSize, 12, 'timestamp size');
        const status = await appearance(page.getByRole('button', { name: /^Status:/ }));
        near(status.width, 280, 'status width'); near(status.height, 24, 'status height'); near(status.fontSize, 14, 'status size');
        assert.equal((await appearance(page.locator('[data-freshness-glyph="dot"]').filter({ visible: true }))).animation, 'none');
        const passkeyLayouts = await measureTrustPasskeys(page, open, mode, screenshot);
        const dashboardLayouts = await measureDashboardOnboarding(page, open);
        return { avatar, timestamp, status, passkeyLayouts, dashboardLayouts };
      });
      await record(`${mode}: bounded motion and reduced-motion suppression`, async () => {
        await page.emulateMedia({ reducedMotion: 'no-preference' }); await open('analytics', 'model');
        const selectors = ['[data-surface-emphasis="dominant"]', '[data-visual="reply-coverage-fill"]']; const animations = [];
        for (const selector of selectors) { const style = await appearance(page.locator(selector).first()); animations.push(style);
          assert.notEqual(style.animation, 'none'); assert.equal(style.iterations, '1');
          assert(parseFloat(style.duration) + parseFloat(style.delay) <= 0.321, 'motion exceeds spatial duration'); }
        await page.emulateMedia({ reducedMotion: 'reduce' });
        for (const selector of selectors) assert.equal((await appearance(page.locator(selector).first())).animation, 'none');
        return animations;
      });
    } finally { await context.close(); }
  }
  const report = { revision: process.env.VISUAL_CAPTURE_REVISION ?? null, checks, failures };
  await writeFile(join(directory, 'acceptance.json'), JSON.stringify(report, null, 2) + '\n');
  return report;
}
