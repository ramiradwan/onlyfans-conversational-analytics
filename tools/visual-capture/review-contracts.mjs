import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

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

/** Separate interaction probes; they never alter a standard capture's state. */
export async function captureReviewChecks(browser, base, outDir) {
  const directory = join(outDir, 'review'); await mkdir(directory, { recursive: true });
  const checks = [], failures = [];
  const record = async (name, action) => {
    try { checks.push({ name, result: 'passed', measurements: await action() }); }
    catch (error) { failures.push(`${name}: ${error.message}`); }
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
        await page.screenshot({ path: join(directory, `rail-hover-${mode}.png`), animations: 'disabled' });
        await page.mouse.move(400, 400); await page.keyboard.press('Tab'); await inbox.focus();
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const focus = await appearance(inbox); assert.deepEqual(focus.fill, hovered.fill); assert.notEqual(focus.background, 'rgba(0, 0, 0, 0.12)');
        assert(focus.focusVisible, 'keyboard focus is missing');
        assert.equal(focus.outlineWidth, '2px'); assert.equal(focus.outlineStyle, 'solid'); assert.equal(focus.outlineOffset, '-2px');
        await page.screenshot({ path: join(directory, `rail-focus-${mode}.png`), animations: 'disabled' });
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
        await page.screenshot({ path: join(directory, `mobile-drawer-${mode}.png`), animations: 'disabled' });
        await page.waitForFunction(() => document.querySelector('#mobile-navigation').contains(document.activeElement));
        await page.keyboard.press('Escape'); await drawer.waitFor({ state: 'hidden' });
        assert(await menu.evaluate((node) => document.activeElement === node), 'Escape lost menu focus');
        const focus = await appearance(menu); assert.notEqual(focus.background, 'rgba(0, 0, 0, 0.12)');
        assert(focus.focusVisible, 'restored menu focus is not visible'); assert.equal(focus.outlineWidth, '2px');
        assert.equal(await menu.locator('.MuiTouchRipple-childPulsate').count(), 0, 'default keyboard ripple obscures the authored focus fill');
        await page.screenshot({ path: join(directory, `mobile-focus-${mode}.png`), animations: 'disabled' });
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
        const status = await appearance(page.getByRole('button', { name: /^Status:/ })); assert.equal(status.border, '1px');
        assert.equal((await appearance(page.locator('[data-status-settled="true"]'))).animation, 'none');
        const passkeyLayouts = [];
        for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
          await page.setViewportSize(viewport);
          await open('home');
          const homeBrand = await appearance(page.locator('[data-visual="brand-tile"]').first());
          await open('passkey', 'resting');
          const banner = page.getByRole('banner');
          const header = await appearance(banner);
          const brand = await appearance(banner.locator('[data-visual="brand-tile"]'));
          const cardLocator = page.locator('[data-visual="passkey-card"]');
          const card = await appearance(cardLocator);
          assert.equal(await cardLocator.locator('[data-visual="brand-tile"]').count(), 0);
          assert.equal(await page.getByRole('main').getByRole('banner').count(), 0);
          assert.equal(await cardLocator.locator('.MuiButton-contained').count(), 1);
          assert(await cardLocator.getByRole('button', { name: 'Sign in with passkey', exact: true }).evaluate(n => n.classList.contains('MuiButton-contained')));
          near(brand.width, 32, 'passkey brand width'); near(brand.height, 32, 'passkey brand height');
          near(brand.y, 20, 'passkey brand top');
          if (viewport.width === 1440) {
            near(brand.x, homeBrand.x, 'passkey/home brand left'); near(brand.y, homeBrand.y, 'passkey/home brand top');
            near(card.y + card.height / 2, viewport.height / 2, 'passkey vertical center', 1);
          } else {
            near(brand.x, 16, 'narrow passkey brand inset');
            assert(card.y >= header.y + header.height, 'passkey overlaps its header');
          }
          const tile = await appearance(page.locator('[data-visual="passkey-lock"]'));
          near(tile.width, 56, 'lock tile'); near(tile.height, 56, 'lock tile'); assert.equal(tile.radius, '18px');
          const heading = await appearance(page.getByRole('heading', { level: 1 }));
          near(heading.fontSize, 26, 'passkey title'); assert(heading.font.includes('Space Grotesk'));
          const setup = cardLocator.getByRole('button', { name: 'Set up a passkey', exact: true });
          for (let index = 0; index < 4 && !await setup.evaluate(n => document.activeElement === n); index++) {
            await page.keyboard.press('Tab');
          }
          assert(await setup.evaluate(n => n.matches(':focus-visible')), 'setup is not keyboard focusable');
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          near(parseFloat((await appearance(setup)).outlineWidth), 2, 'setup keyboard focus ring');
          await page.screenshot({ path: join(directory, `passkey-layout-${mode}-${viewport.width}.png`), animations: 'disabled' });
          passkeyLayouts.push({ viewport, header, brand, homeBrand, card, tile, heading });
        }
        return { avatar, timestamp, status, passkeyLayouts };
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
