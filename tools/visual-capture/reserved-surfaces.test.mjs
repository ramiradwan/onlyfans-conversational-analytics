import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { chromium } from 'playwright';
import { onboardingStaticAssets, onboardingStaticFixtures } from './onboarding-static-fixtures.mjs';
import { BROWSER_TASK_STATES, DESKTOP_ONBOARDING_STATES } from './onboarding-surface-states.mjs';
import { observeStaticPaint } from './static-surfaces.mjs';
import { CANVAS_DELTA, LAYOUT_SHIFT, colorDistance, grade, gradeLayoutShifts } from './stability-contracts.mjs';
import { inspectTaskCopy } from './task-copy.mjs';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
for (const surface of ['popup', 'setup', 'options']) {
  test(`${surface} retains feedback and content frames from its first document`, async () => {
    const html = await readFile(new URL(`../../extension/${surface}.html`, import.meta.url), 'utf8');
    const dom = new JSDOM(html);
    try {
      const document = dom.window.document;
      assert(document.querySelector('[data-reserved-region]'));
      assert(document.querySelector('#feedback').closest('[data-reserved-region]'));
      assert.equal(document.querySelector('[data-reserved-region].hidden'), null);
      if (surface === 'popup') assert(document.querySelector('[data-reserved-region="popup-status"] [data-region-content]'));
      if (surface === 'options') assert(document.querySelector('#connection-details-dialog'));
    } finally { dom.window.close(); }
  });
}

test('native recovery needs no package input and keeps setup-code receiving separate', async () => {
  const html = await readFile(new URL('../../frontend/onboarding/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html);
  try {
    const document = dom.window.document;
    assert.equal(document.querySelectorAll('[data-step]').length, 3);
    assert.equal(document.querySelector('#recovery-package'), null);
    assert.equal(document.querySelector('textarea'), null);
    assert.equal(document.querySelector('#task-status').getAttribute('role'), 'status');
    assert.equal(document.querySelector('#task-action').getAttribute('aria-describedby'), 'task-description task-status');
    assert.equal(document.querySelector('#setup-code-open').textContent, 'I have a setup code');
    assert.equal(document.querySelector('#setup-code-open').hidden, true);
  } finally { dom.window.close(); }
});

async function withDesktop(width, scale, mode, name, run) {
  await withStaticSurface('desktop-onboarding', width, scale, mode, name, run);
}

async function withStaticSurface(surface, width, scale, mode, name, run, firstPaint) {
  const [assets, fixtures] = await Promise.all([onboardingStaticAssets(), onboardingStaticFixtures()]);
  const fixture = fixtures.find(item => item.surface === surface && item.name === name);
  const origin = 'http://static-visual.localhost';
  const browser = await chromium.launch();
  let release;
  const delivery = new Promise(resolve => { release = resolve; });
  try {
    const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: mode, reducedMotion: 'reduce' });
    await page.addInitScript(observeStaticPaint, 'h1, header, nav, .brand-mark, button');
    await page.addInitScript(({ state, mode }) => {
      localStorage.setItem('mui-mode', mode);
      window.__staticSurfaceFixture = state;
    }, { state: fixture.state, mode });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      if (url.pathname === fixture.urlPath) return route.fulfill({ contentType: 'text/html', body: fixture.html });
      if (url.pathname === '/' + surface + '.js') {
        if (firstPaint) await delivery;
        return route.fulfill({ contentType: 'text/javascript', body: fixture.script });
      }
      return assets[url.pathname] ? route.fulfill(assets[url.pathname]) : route.abort();
    });
    await page.goto(origin + fixture.urlPath, { waitUntil: firstPaint ? 'commit' : 'load' });
    if (firstPaint) await firstPaint(page);
    release();
    await page.locator('main[data-ready="true"]').waitFor();
    await page.evaluate(async scale => {
      await document.fonts.ready;
      if (scale !== 1) document.documentElement.style.fontSize = `${16 * scale}px`;
    }, scale);
    const render = async state => {
      await page.evaluate(state => { window.__staticSurfaceFixture = state; }, state);
      await page.addScriptTag({ content: fixture.script });
    };
    await run(page, render);
  } finally {
    release();
    await browser.close();
  }
}

test('desktop empty task actions occupy no more than one line', async () => {
  await withDesktop(1440, 1, 'light', 'finish_getting_ready', async page => {
    const region = await page.locator('[data-reserved-region="desktop-task-actions"]').evaluate(node => ({
      height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight),
    }));
    assert(region.height <= region.line, `Empty desktop action region occupies ${region.height}px`);
  });
});

for (const [width, scale, mode] of [[1440, 1, 'light'], [390, 1, 'dark'], [320, 2, 'light']]) {
  test(`desktop empty status occupies no space at ${width}px and ${scale}× text`, async () => {
    await withDesktop(width, scale, mode, 'loading', async (page, render) => {
      const status = page.locator('#task-status');
      const states = DESKTOP_ONBOARDING_STATES;
      for (const state of [states.loading, states.browser_access_needed, states.account_confirm,
        states.finish_secure_app_setup, states.finish_connect_extension, { ...states.loading, error: 'session_unavailable' }]) {
        await render(state);
        assert.equal(await status.textContent(), '');
        assert.deepEqual(await status.evaluate(node => {
          const style = getComputedStyle(node);
          return [node.getBoundingClientRect().height, style.marginBlockStart, style.marginBlockEnd];
        }), [0, '0px', '0px']);
      }
    });
  });
}

test('desktop status draws the outlined mark for each owner tone', async () => {
  await withDesktop(1440, 1, 'light', 'browser_access_connecting', async (page, render) => {
    const states = DESKTOP_ONBOARDING_STATES;
    const signIn = states.account_sign_in_in_progress;
    const retired = { ...signIn, pending: null, snapshot: { ...signIn.snapshot, pending_operations: [],
      authentication: { ...signIn.snapshot.authentication, state: 'retired' } } };
    const cases = [
      [states.browser_access_connecting, '#task-pending', 'progress', 'pending', '--dipsy-color-primary'],
      [states.browser_access_extension_unavailable, '#task-status', 'warning', 'attention', '--dipsy-color-text-secondary'],
      [{ ...states.loading, error: 'connection_unavailable' }, '#task-status', 'error', 'attention', '--dipsy-color-error'],
      [retired, '#task-status', 'neutral', 'info', '--dipsy-color-text-secondary'],
      [states.passkey_sign_in_required, '#local-auth-status', 'success', 'done', '--dipsy-color-primary'],
    ];
    for (const [state, selector, tone, kind, token] of cases) {
      await render(state);
      const row = page.locator(selector);
      assert.equal(await row.getAttribute('data-tone'), tone);
      assert.deepEqual(await row.evaluate((node, token) => {
        const icon = node.querySelector('svg');
        if (!icon) return null;
        const probe = document.createElement('span');
        probe.style.color = `var(${token})`;
        document.body.append(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        const { width, height } = icon.getBoundingClientRect();
        return [icon.dataset.statusIcon, width, height, getComputedStyle(icon).color === color,
          getComputedStyle(node, '::before').content, icon.getAttribute('aria-hidden')];
      }, token), [kind, 20, 20, true, 'none', 'true']);
    }
    await render(states.browser_access_connecting);
    const row = page.locator('#task-pending');
    const arc = row.locator('svg[data-status-icon="pending"]');
    assert.equal((await arc.boundingBox()).y - (await row.boundingBox()).y, 12);
    assert.equal(await arc.evaluate(node => getComputedStyle(node).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    assert.equal(await arc.evaluate(node => getComputedStyle(node).animationName), 'status-rotate');
  });
});

test('desktop step labels and branding fit at 320px and 2× text', async () => {
  await withDesktop(320, 2, 'dark', 'browser_access_needed', async (page, render) => {
    for (const state of [DESKTOP_ONBOARDING_STATES.browser_access_needed, DESKTOP_ONBOARDING_STATES.account_confirm,
      DESKTOP_ONBOARDING_STATES.finish_secure_app_setup]) {
      await render(state);
      const geometry = await page.locator('.setup-steps').evaluate(list => {
        const steps = [...list.children];
        const number = steps[0].querySelector('.setup-step-badge span');
        const labels = steps.map(step => [step.matches('[aria-current="step"]'),
          getComputedStyle(step.querySelector('.setup-step-label')).clipPath === 'none']);
        const markCenters = steps.map(step => {
          const mark = step.querySelector('.setup-step-badge').getBoundingClientRect();
          return mark.y + mark.height / 2;
        });
        return { labels, markCenters, digitHeight: number.getBoundingClientRect().height,
          pageFits: document.documentElement.scrollWidth <= innerWidth };
      });
      assert(geometry.labels.every(([current, visible]) => current === visible));
      assert(geometry.markCenters.every(center => center === geometry.markCenters[0]));
      assert(geometry.digitHeight <= 24);
      assert(geometry.pageFits);
    }
    assert.match(await page.getByRole('navigation').ariaSnapshot(), /Set up Chrome[\s\S]*Connect your account[\s\S]*Finish setup/u);
  });
});

test('desktop loading and unavailable cards use the specified tokens and marks', async () => {
  await withDesktop(1440, 1, 'dark', 'loading', async (page, render) => {
    const colors = await page.locator('.setup-task-card').evaluate(card => {
      const probe = document.createElement('span');
      probe.style.background = 'var(--dipsy-intent-surface-placeholder)';
      card.append(probe);
      const fill = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return ['#task-step', '#task-title', '.desktop-loading-copy span', '.desktop-loading-action']
        .map(selector => getComputedStyle(card.querySelector(selector)).backgroundColor === fill);
    });
    assert.deepEqual(colors, [true, true, true, true]);
    await render({ ...DESKTOP_ONBOARDING_STATES.loading, error: 'session_unavailable' });
    assert.equal(await page.locator('.desktop-task-icon svg:visible').getAttribute('data-task-icon'), 'app');
    assert.deepEqual(await page.locator('#task-step').evaluate(node => {
      const style = getComputedStyle(node);
      return [style.textTransform, style.letterSpacing];
    }), ['uppercase', '0.96px']);
    assert.equal(await page.locator('#task-title').evaluate(node => getComputedStyle(node).textWrapStyle), 'balance');
  });
});

test('desktop sign-in link has room for focus and a shared left edge', async () => {
  await withDesktop(390, 1, 'dark', 'account_confirm', async (page, render) => {
    const state = DESKTOP_ONBOARDING_STATES.account_confirm;
    const link = page.locator('#sign-in-switch');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    assert(await link.evaluate(node => node.matches(':focus-visible')));
    const provider = page.locator('#decision-provider');
    assert.match(await provider.evaluate(node => getComputedStyle(node, '::after').content), /·/u);
    const proof = await provider.boundingBox();
    const action = await link.boundingBox();
    const outline = await link.evaluate(node => {
      const style = getComputedStyle(node);
      return parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
    });
    assert(action.x - outline > proof.x + proof.width);
    await render({ ...state, snapshot: { ...state.snapshot,
      authentication: { ...state.snapshot.authentication, authentication_attempt: null } } });
    assert(await provider.isHidden());
    assert.equal((await link.boundingBox()).x, (await page.locator('#task-title').boundingBox()).x);
    assert.equal(await link.evaluate(node => getComputedStyle(node).paddingInlineStart), '0px');
  });
});

test('desktop marks and pending swaps stay aligned through current-step and reason changes', async () => {
  await withDesktop(1440, 1, 'light', 'browser_access_needed', async (page, render) => {
    await page.waitForFunction(() => window.__staticPaint.first !== null);
    const { first, canvas, shifts } = await page.evaluate(() => ({ ...window.__staticPaint, canvas: getComputedStyle(document.body).backgroundColor }));
    assert.equal(first.faces.find(face => face.family === 'Inter Variable')?.status, 'loaded');
    assert.notEqual(grade(colorDistance(first.canvas, canvas), CANVAS_DELTA), 'fail');
    assert.notEqual(gradeLayoutShifts(shifts, LAYOUT_SHIFT).level, 'fail');
    await page.keyboard.press('Tab');
    assert(await page.locator('#task-action').evaluate(node => node.matches(':focus-visible')));
    const marks = () => page.locator('.setup-step-badge').evaluateAll(nodes => nodes.map(node => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { x, y, width, height };
    }));
    const original = await marks();
    assert(original.every(mark => mark.width === 28 && mark.height === 28 && mark.y === original[0].y));
    const stepper = await page.locator('.setup-steps li').first().evaluate(node => ({
      y: node.getBoundingClientRect().y, height: node.getBoundingClientRect().height,
      align: getComputedStyle(node).alignItems, connector: getComputedStyle(node, '::after').height,
    }));
    assert.deepEqual([stepper.align, stepper.connector], ['center', '1px']);
    assert.equal(stepper.y + stepper.height / 2, original[0].y + 14);
    const pending = DESKTOP_ONBOARDING_STATES.browser_access_connecting;
    await render(pending);
    const row = async id => {
      const { y, height } = await page.locator(id).boundingBox();
      return { y, height };
    };
    const waiting = await row('#task-pending');
    assert.equal(await page.locator('#task-pending').evaluate(node => getComputedStyle(node, '::before').animationName), 'none');
    const opened = { ...pending, acting: null };
    await render(opened);
    assert.deepEqual(await row('#browser-reopen'), waiting);
    await render({ ...opened, error: 'extension_unavailable' });
    assert.deepEqual(await row('#browser-reopen'), waiting);
    await render({ ...opened, connected: false, error: 'status_unavailable' });
    assert.deepEqual(await row('#task-pending'), waiting);
    await render(DESKTOP_ONBOARDING_STATES.account_confirm);
    assert.deepEqual(await marks(), original);
    await render(DESKTOP_ONBOARDING_STATES.finish_connect_extension);
    assert.deepEqual(await marks(), original);
    const connect = await row('#task-action');
    await render(DESKTOP_ONBOARDING_STATES.finish_connecting_extension);
    assert.deepEqual(await row('#task-pending'), connect);
    await render(DESKTOP_ONBOARDING_STATES.finish_secure_app_setup);
    const create = await row('#local-authenticate');
    await render(DESKTOP_ONBOARDING_STATES.finish_passkey_creating);
    assert.deepEqual(await row('#local-auth-pending'), create);
  });
});

test('desktop titles wrap freely and narrow step names remain accessible at 320px and 2× text', async () => {
  await withDesktop(320, 2, 'dark', 'finish_secure_app_setup', async page => {
    const result = await page.locator('#task-title').evaluate(node => {
      const css = getComputedStyle(node);
      const range = document.createRange();
      range.selectNodeContents(node);
      const lines = [...range.getClientRects()];
      return { count: lines.length, clamp: css.webkitLineClamp,
        fits: lines.every(rect => rect.left >= 0 && rect.right <= innerWidth) && node.scrollHeight <= node.clientHeight,
        pageFits: document.documentElement.scrollWidth <= innerWidth };
    });
    assert(result.count > 2 && result.fits && result.pageFits && result.clamp === 'none', JSON.stringify(result));
    assert.match(await page.getByRole('navigation').ariaSnapshot(), /Set up Chrome[\s\S]*Connect your account[\s\S]*Finish setup/u);
  });
});

test('desktop task copy fits the 1440px budgets in both color schemes', async () => {
  for (const mode of ['light', 'dark']) {
    await withDesktop(1440, 1, mode, 'browser_access_needed', async (page, render) => {
      for (const [name, state] of Object.entries(DESKTOP_ONBOARDING_STATES)) {
        await render(state);
        const lines = await page.evaluate(() => Object.fromEntries([
          'task-title', 'task-description', 'task-status', 'task-pending', 'local-auth-status', 'local-auth-pending',
        ].map(id => {
          const node = document.getElementById(id);
          const range = document.createRange();
          const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
          const tops = new Set();
          while (walker.nextNode()) {
            range.selectNodeContents(walker.currentNode);
            for (const rect of range.getClientRects()) tops.add(rect.top);
          }
          return [id, tops.size];
        })));
        for (const [id, count] of Object.entries(lines)) {
          assert(count <= (id === 'task-description' ? 2 : 1), `${mode} ${name} ${id} wraps to ${count} lines`);
        }
      }
    });
  }
});

test('native recovery aligns the outlined pending icon in the Trust action row', async () => {
  await withDesktop(1440, 1, 'light', 'recovery_preparing', async page => {
    const row = page.locator('#task-pending');
    const icon = row.locator('svg[data-status-icon="pending"]');
    const bounds = await row.boundingBox();
    const mark = await icon.boundingBox();
    assert.equal(bounds.height, 48);
    assert.equal(mark.width, 20);
    assert.equal(mark.height, 20);
    assert.equal(mark.y - bounds.y, 14);
    assert.equal(await row.evaluate(node => getComputedStyle(node, '::before').content), 'none');
    assert.equal(await icon.evaluate(node => getComputedStyle(node).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    assert.equal(await icon.evaluate(node => getComputedStyle(node).animationName), 'status-rotate');
  });
});

for (const mode of ['light', 'dark']) {
  test(`Chrome task placeholders and narrow handoffs match the boards in ${mode}`, async () => {
    await withStaticSurface('browser-task', 1440, 1, mode, 'loading', async (page, render) => {
      for (const [width, size] of [[1440, 16], [390, 16], [320, 32]]) {
        await page.setViewportSize({ width, height: 900 });
        await page.evaluate(size => { document.documentElement.style.fontSize = size + 'px'; }, size);
        await render(BROWSER_TASK_STATES.loading);
        const loading = page.getByRole('region', { name: 'Loading browser task' });
        assert.equal(await loading.getAttribute('aria-busy'), 'true');
        assert(await loading.getByRole('heading', { name: 'Loading browser task' }).count());
        const metrics = await loading.evaluate(card => {
          const heading = card.querySelector('h1');
          const title = heading.getBoundingClientRect();
          const bar = getComputedStyle(heading, '::before');
          const eyebrow = card.querySelector('.setup-eyebrow').getBoundingClientRect();
          const spot = card.querySelector('.browser-task-spot').getBoundingClientRect();
          return {
            titleHeight: title.height, lineHeight: parseFloat(getComputedStyle(heading).lineHeight),
            titleWidth: title.width, barWidth: parseFloat(bar.width), barColor: bar.backgroundColor,
            actionColor: getComputedStyle(card.querySelector('.browser-loading-action')).backgroundColor,
            token: getComputedStyle(card).getPropertyValue('--dipsy-intent-surface-placeholder').trim(),
            eyebrowCenter: eyebrow.top + eyebrow.height / 2, spotCenter: spot.top + spot.height / 2,
          };
        });
        assert(Math.abs(metrics.titleHeight - metrics.lineHeight) < 1, JSON.stringify({ width, ...metrics }));
        assert(Math.abs(metrics.barWidth - metrics.titleWidth * .64) < 1);
        assert(Math.abs(metrics.eyebrowCenter - metrics.spotCenter) < 1);
        assert(colorDistance(metrics.barColor, metrics.token) < .001);
        assert(colorDistance(metrics.actionColor, metrics.token) < .001);

        if (width > 420) continue;
        for (const name of ['extension_disconnected', 'profile_mismatch', 'account_mismatch', 'reported',
          'task_completed', 'task_expired', 'task_changed', 'service_unavailable']) {
          await render(BROWSER_TASK_STATES[name]);
          const handoff = await page.locator('#desktop-return').evaluate(link => {
            const actions = link.parentElement;
            const card = link.closest('.setup-task-card');
            return [link.getBoundingClientRect().width, actions.getBoundingClientRect().width, getComputedStyle(card).paddingInlineStart];
          });
          assert.deepEqual(handoff, [handoff[1], handoff[1], '20px'], `${width} ${name}`);
        }
      }
    });
  });

  test(`Chrome task copy and waiting swaps fit at 1440px in ${mode}`, async () => {
    await withStaticSurface('browser-task', 1440, 1, mode, 'auth_idle', async (page, render) => {
      const heading = page.locator('#task-title');
      const card = await page.locator('.setup-task-card').elementHandle();
      const box = async selector => {
        const { y, height } = await page.locator(selector).boundingBox();
        return { y, height };
      };
      await page.keyboard.press('Tab');
      const action = page.locator('#authentication-open');
      assert(await action.evaluate(node => node.matches(':focus-visible') && getComputedStyle(node).outlineWidth === '2px'));
      const button = await box('#authentication-open');
      const title = await box('#task-title');
      await render(BROWSER_TASK_STATES.auth_opening);
      assert.equal(await heading.textContent(), 'Signing in…');
      assert(await card.evaluate(node => node.isConnected));
      assert.deepEqual(await box('#task-title'), title);
      assert.deepEqual(await box('#task-status'), button);
      assert.equal(await page.locator('#task-status').evaluate(node => getComputedStyle(node, '::before').animationName), 'none');
      await render(BROWSER_TASK_STATES.pairing_saved);
      assert.equal(await card.evaluate(node => node.isConnected), false);

      await render(BROWSER_TASK_STATES.auth_checking);
      await page.keyboard.press('Tab');
      const handoff = page.locator('#desktop-return');
      const waiting = await box('#task-status');
      const desktop = await box('#desktop-return');
      await render(BROWSER_TASK_STATES.auth_unconfirmed);
      assert.deepEqual(await box('#task-status'), waiting);
      assert.deepEqual(await box('#desktop-return'), desktop);
      assert(await handoff.evaluate(node => node.matches(':focus-visible')));

      for (const [name, state] of Object.entries(BROWSER_TASK_STATES)) {
        await render(state);
        await inspectTaskCopy(page, { surface: 'browser-task', name, state });
        const lines = await page.locator('#task-title, #task-description, #task-message').evaluateAll(nodes => nodes.map(node => {
          const range = document.createRange();
          range.selectNodeContents(node);
          return [node.id, new Set([...range.getClientRects()].map(rect => rect.top)).size];
        }));
        for (const [id, count] of lines) {
          assert(count <= (id === 'task-description' ? 2 : 1), `${mode} ${name} ${id} wraps to ${count} lines`);
        }
      }
    }, async page => {
      await page.waitForFunction(() => window.__staticPaint.first !== null);
      const card = page.getByRole('region', { name: 'Loading browser task' });
      await card.waitFor();
      assert.equal(await card.getAttribute('aria-busy'), 'true');
      assert.equal(await card.locator('button:visible, a:visible').count(), 0);
      const paint = await page.evaluate(() => ({
        first: window.__staticPaint.first,
        canvas: getComputedStyle(document.body).backgroundColor,
      }));
      assert.equal(paint.first.faces.find(face => face.family === 'Inter Variable')?.status, 'loaded');
      assert.notEqual(grade(colorDistance(paint.first.canvas, paint.canvas), CANVAS_DELTA), 'fail');
    });
  });
}

test('Chrome task cards use the specified spot icons, quiet guidance and pending rows', async () => {
  await withStaticSurface('browser-task', 1440, 1, 'light', 'needs_consent', async (page, render) => {
    const expectedSpots = {
      unavailable: 'extension', extension_disconnected: 'app', needs_consent: 'extension', needs_permission: 'extension',
      needs_account: 'account', account_mismatch: 'account', pairing_saved: 'link', auth_idle: 'account',
      auth_opening: 'account', paused: 'pause', ready: 'browser',
    };
    for (const [name, expected] of Object.entries(expectedSpots)) {
      await render(BROWSER_TASK_STATES[name]);
      const spot = page.locator('.browser-task-spot');
      assert(await spot.locator('svg').isVisible(), name);
      const icons = await spot.locator('path').evaluateAll(paths =>
        paths.filter(path => getComputedStyle(path).display !== 'none').map(path => path.dataset.spot));
      assert.deepEqual(icons, [expected], name);
      assert.equal(await spot.evaluate(node => getComputedStyle(node, '::before').content), 'none');
    }

    for (const name of ['task_changed', 'task_expired', 'needs_consent', 'needs_permission', 'needs_account', 'pairing_saved']) {
      await render(BROWSER_TASK_STATES[name]);
      const row = page.getByRole('status');
      assert(await row.isVisible(), name);
      assert.equal(await row.getAttribute('data-tone'), 'none', name);
      assert.equal(await row.locator('svg:visible').count(), 0, name);
      const color = await row.evaluate(node => {
        const css = getComputedStyle(node);
        return [css.color, css.getPropertyValue('--dipsy-color-text-secondary').trim()];
      });
      assert(colorDistance(...color) < .001, name);
    }
    assert.deepEqual(await page.locator('.browser-task-header .setup-eyebrow, #task-title').evaluateAll(([eyebrow, title]) => [
      getComputedStyle(eyebrow).textTransform,
      parseFloat(getComputedStyle(eyebrow).letterSpacing) / parseFloat(getComputedStyle(eyebrow).fontSize),
      getComputedStyle(title).textWrapStyle,
    ]), ['uppercase', .08, 'balance']);

    for (const name of ['auth_opening', 'paused', 'status_reconnecting', 'pairing_connecting',
      'auth_checking', 'auth_unconfirmed', 'pairing_checking']) {
      await render(BROWSER_TASK_STATES[name]);
      const row = page.getByRole('status');
      const icon = row.locator('svg');
      assert(await icon.isVisible(), name);
      const arc = icon.locator('circle');
      assert.equal(await arc.getAttribute('r'), '8');
      assert.equal(await arc.getAttribute('stroke-width'), '2.2');
      assert.equal(await arc.getAttribute('stroke-linecap'), 'round');
      assert.equal(await icon.evaluate(node => getComputedStyle(node).animationName), 'none');
      const { width, height } = await icon.boundingBox();
      assert.deepEqual([width, height], [20, 20]);
      const { y, height: rowHeight } = await row.boundingBox();
      const anchor = page.locator(name.endsWith('checking') || name === 'auth_unconfirmed' ? '#desktop-return' : '#task-title');
      const anchorBox = await anchor.boundingBox();
      const gap = name.endsWith('checking') || name === 'auth_unconfirmed' ? 16 : 24;
      assert.deepEqual([rowHeight, y - anchorBox.y - anchorBox.height], [44, gap], name);
    }

    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await render(BROWSER_TASK_STATES.paused);
    assert.equal(await page.locator('#task-status svg').evaluate(node => getComputedStyle(node).animationName), 'status-rotate');
    const motion = await page.locator('.setup-task-card').evaluate(async node => {
      await new Promise(requestAnimationFrame);
      const arrival = node.getAnimations().find(animation => animation.animationName === 'browser-task-arrive');
      const [first] = arrival.effect.getKeyframes();
      return [first.opacity, first.transform, first.easing, arrival.effect.getTiming().duration];
    });
    assert.deepEqual(motion, ['0', 'translateY(8px)', 'cubic-bezier(0, 0, 0, 1)', 200]);
  });
});

test('Chrome task copy wraps freely at 320px and 2× text', async () => {
  await withStaticSurface('browser-task', 320, 2, 'dark', 'pairing_saved', async (page, render) => {
    await render(BROWSER_TASK_STATES.auth_idle);
    const signIn = await page.locator('.setup-task-card').elementHandle();
    await render(BROWSER_TASK_STATES.auth_opening);
    assert.equal(await signIn.evaluate(node => node.isConnected), false);
    let wrapped = false;
    for (const [name, state] of Object.entries(BROWSER_TASK_STATES)) {
      await render(state);
      await inspectTaskCopy(page, { surface: 'browser-task', name, state });
      const result = await page.locator('#task-title, #task-message:not(:empty)').evaluateAll(nodes => nodes.map(node => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const lines = [...range.getClientRects()];
        const slot = node.closest('#task-status') ?? node;
        return { count: lines.length, clamp: getComputedStyle(node).webkitLineClamp,
          fits: lines.every(rect => rect.left >= 0 && rect.right <= innerWidth) && slot.scrollHeight <= slot.clientHeight,
          pageFits: document.documentElement.scrollWidth <= innerWidth };
      }));
      assert(result.every(item => item.fits && item.pageFits && item.clamp === 'none'), `${name} ${JSON.stringify(result)}`);
      wrapped ||= result.some(item => item.count > 2);
    }
    assert(wrapped);
    await render(BROWSER_TASK_STATES.ready);
    assert.equal(await page.locator('.task-actions-slot').evaluate(node => node.getBoundingClientRect().height), 0);
  });
});
