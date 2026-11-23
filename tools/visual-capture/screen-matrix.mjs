import assert from 'node:assert/strict';
import { openActivationReview, publishActivation } from './desktop-decision-fixtures.mjs';

export const FIXED_NOW = '2026-06-30T12:05:00.000Z';
export const MODES = ['light', 'dark'];
export const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'narrow', width: 390, height: 844 },
  { name: 'tablet', width: 820, height: 900, targetedOnly: true },
  { name: 'narrow-2x', width: 320, height: 900, targetedOnly: true },
];

const heading = (name) => (page) => page.getByRole('heading', { level: 1, name });
const text = (value) => (page) => page.getByText(value, { exact: true }).first();
const openPairingComparison = async page => {
  await page.getByRole('button', { name: 'Connection options', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Compare connection codes', exact: true }).click();
};

const findingHeadline = 'One topic appears in 50% of fully checked messages';
const longFinding = 'Custom content requests, pricing details, and delivery expectations for scheduled sessions';
const findingsDialog = page => page.getByRole('dialog', { name: 'All topics', exact: true });
const findingsTrigger = page => page.getByRole('button', { name: 'See all topics', exact: true });

export async function prepareFindings(page, longCopy = false) {
  await page.route('**/src/story-only/findingsFixtures.ts*', async route => {
    const response = await route.fetch();
    const source = await response.text();
    if (!source.includes('Example topic')) throw new Error('Synthetic findings label is missing');
    const body = source.replace('Example topic', 'Pricing questions');
    await route.fulfill({ response, body: longCopy ? body.replaceAll('Second category', longFinding) : body });
  });
  await page.addInitScript(() => {
    if (innerWidth === 320) {
      const root = new MutationObserver(() => {
        if (!document.documentElement) return;
        document.documentElement.style.fontSize = '200%';
        root.disconnect();
      });
      root.observe(document, { childList: true, subtree: true });
    }
  });
}

async function assertFindingTitle(locator, viewport) {
  const layout = await locator.evaluate(node => {
    const css = getComputedStyle(node);
    const range = document.createRange();
    range.selectNodeContents(node);
    return { lines: new Set([...range.getClientRects()].map(rect => rect.top)).size,
      width: node.clientWidth, font: css.fontFamily, fontSize: parseFloat(css.fontSize), clamp: css.webkitLineClamp };
  });
  if (!layout.font.includes('Inter Variable') || layout.clamp !== 'none') throw new Error('Finding title lost its wrapping or font');
  if (viewport.width === 1440 && layout.lines !== 1) {
    throw new Error(`Finding title uses ${layout.lines} lines in ${layout.width}px and has a one-line budget`);
  }
  if (viewport.width === 320 && (layout.lines < 2 || layout.fontSize < 48)) throw new Error('Finding title did not wrap at 2×');
}

export async function measureFindingCopy(page, viewport, state = 'first') {
  const card = page.getByRole('region', { name: state === 'first' ? 'Your first insights are ready' : 'Insights', exact: true });
  const measure = async locator => {
    assert.equal(await locator.count(), 1, 'Expected one findings text element');
    assert(await locator.isVisible(), 'Findings text must be visible');
    return locator.evaluate(node => {
      const css = getComputedStyle(node);
      const range = document.createRange();
      range.selectNodeContents(node);
      const rects = [...range.getClientRects()];
      const bounds = node.getBoundingClientRect();
      const card = node.closest('[data-visual="findings-summary"]').getBoundingClientRect();
      const fits = rects.length > 0 && rects.every(rect => rect.left >= Math.max(0, bounds.left, card.left) - 0.5
        && rect.right <= Math.min(innerWidth, bounds.right, card.right) + 0.5
        && rect.top >= bounds.top - 0.5 && rect.bottom <= bounds.bottom + 0.5);
      return { text: node.textContent, lines: new Set(rects.map(rect => rect.top)).size,
        x: bounds.x, y: bounds.y + scrollY, width: bounds.width, height: bounds.height,
        fits, font: css.fontFamily, fontSize: parseFloat(css.fontSize), clamp: css.webkitLineClamp,
        overflow: css.overflow, ellipsis: css.textOverflow, color: css.color, lineHeight: parseFloat(css.lineHeight) };
    });
  };
  const check = (measurement, label, budget) => {
    assert(measurement.fits && measurement.font.includes('Inter Variable') && measurement.clamp === 'none'
      && measurement.overflow === 'visible' && measurement.ellipsis === 'clip', `${label} lost readable wrapping`);
    if (viewport.width === 1440) {
      assert(measurement.lines <= budget, `${label} uses ${measurement.lines} lines in ${measurement.width}px (budget ${budget})`);
    }
  };
  const heading = card.getByRole('heading', { level: 2 });
  await heading.waitFor({ state: 'visible' });
  const title = await measure(heading);
  check(title, 'Findings title', 1);
  if (state === 'pending') {
    const status = await measure(card.getByText('Saved findings aren’t ready yet.', { exact: true }));
    check(status, 'Pending findings status', 1);
    assert.equal(await card.locator('[data-visual="finding-bar"], [role="progressbar"]').count(), 0);
    assert.doesNotMatch(await card.innerText(), /\d\s*%|fully checked|being updated|updates by itself/i);
    return { title, status };
  }
  const finding = await measure(card.getByText(longFinding, { exact: true }));
  check(finding, 'Long finding', 2);
  const headline = card.getByRole('heading', { name: findingHeadline, exact: true });
  await assertFindingTitle(headline, viewport);
  const scope = await measure(card.getByText(/^Available message dates:.*All directions.*All conversations$/));
  const basis = await measure(card.getByText('Topics are based on 2 fully checked messages.', { exact: true }));
  const status = await measure(card.getByText(/^Processing state when saved: paused/));
  check(scope, 'Saved findings scope', 2);
  check(basis, 'Saved findings basis', 2);
  check(status, 'Saved findings status', 1);
  for (const paragraph of [scope, basis, status]) {
    assert.equal(paragraph.color, title.color, 'Saved metadata must use secondary ink');
  }
  for (const [before, after] of [[scope, basis], [basis, status]]) {
    const gap = after.y - before.y - before.height;
    assert(gap > before.lineHeight - before.fontSize, 'Saved metadata needs more space between paragraphs than between lines');
  }
  if (viewport.width === 1440) assert(scope.lines + basis.lines <= 2, 'Saved scope and basis exceed two lines');
  if (viewport.width === 320) assert(finding.lines > 2 && finding.fontSize >= 24, 'Long finding did not wrap at 2×');
  const row = card.getByRole('listitem').filter({ hasText: longFinding });
  const share = await row.getByText('50%', { exact: true }).boundingBox();
  assert(share && finding.x + finding.width <= share.x + 0.5, 'Long finding overlaps its saved share');
  return { title, headline: await measure(headline), finding, scope, basis, status };
}

/** Each screen names the locator that proves its state rendered before capture. */
export function createScreens({ assertMaxWidth, assertCentered, assertLoadingGeometry, assertMetricHierarchy } = {}) {
return [
  {
    workspace: 'findings', state: 'first-insights', viewports: ['desktop', 'narrow', 'narrow-2x'],
    query: { stability: 'findings' }, prepare: page => prepareFindings(page, true),
    ready: page => page.getByRole('heading', { name: findingHeadline, exact: true }),
    assert: async (page, viewport) => {
      await measureFindingCopy(page, viewport);
      const bar = await page.locator('[data-visual="finding-bar"]').first().evaluate(node => ({
        animation: getComputedStyle(node).animationName,
        fill: node.getBoundingClientRect().width / node.parentElement.getBoundingClientRect().width,
      }));
      if (bar.animation !== 'none' || Math.abs(bar.fill - 0.5) > 0.005) throw new Error('Finding bar missed its reduced-motion end state');
    },
  },
  {
    workspace: 'findings', state: 'all-topics', viewports: ['desktop', 'narrow', 'narrow-2x'],
    query: { stability: 'findings' }, prepare: page => prepareFindings(page),
    act: async page => {
      await findingsTrigger(page).press('Enter');
      const close = findingsDialog(page).getByRole('button', { name: 'Close', exact: true });
      await close.waitFor({ state: 'visible' });
      await page.keyboard.press('Escape');
      await findingsDialog(page).waitFor({ state: 'hidden' });
      if (!await findingsTrigger(page).evaluate(node => node === document.activeElement && node.matches(':focus-visible'))) {
        throw new Error('All topics did not return keyboard focus');
      }
      await page.keyboard.press('Enter');
    },
    ready: findingsDialog,
    assert: async (page, viewport) => {
      const dialog = findingsDialog(page);
      const basis = dialog.getByText('Based on 2 fully checked messages.', { exact: true });
      await basis.waitFor({ state: 'visible' });
      await dialog.getByRole('heading', { name: 'Mentions', exact: true }).waitFor({ state: 'visible' });
      const close = dialog.getByRole('button', { name: 'Close', exact: true });
      if (Math.abs((await close.boundingBox()).x - (await basis.boundingBox()).x) > 1) {
        throw new Error('All topics actions and content lost their shared left edge');
      }
      const tableArea = dialog.getByRole('group', { name: 'Topics findings · Subjects scroll area', exact: true });
      const table = await tableArea.evaluate(node => {
        const header = node.querySelector('thead').getBoundingClientRect();
        const data = node.querySelector('tbody tr').getBoundingClientRect();
        const view = node.getBoundingClientRect();
        return { horizontal: node.scrollWidth > node.clientWidth, headerHeight: header.height,
          visibleRow: data.top >= header.bottom && data.top < view.bottom };
      });
      if (!table.visibleRow) throw new Error(`All topics header (${table.headerHeight}px) obscures the first data row`);
      if (viewport.width <= 390) {
        if (!table.horizontal) throw new Error('All topics table lost its horizontal scroll area');
        await tableArea.press('ArrowRight');
        await page.waitForFunction(node => node.scrollLeft > 0, await tableArea.elementHandle(), { timeout: 3000 });
        await tableArea.evaluate(node => { node.scrollLeft = 0; });
        await dialog.focus();
      }
      await page.keyboard.press('Shift+Tab');
      if (!await close.evaluate(node => node === document.activeElement && node.matches(':focus-visible'))) {
        throw new Error('All topics did not show keyboard focus on Close');
      }
    },
  },
  ...['current', 'pending', 'unavailable', 'degraded'].map((state) => ({ workspace: 'graph', state,
    ready: heading('Graph explorer'), act: (page) => page.evaluate((status) => window.__workspaceFixture.projection(status), state) })),
  { workspace: 'home', state: 'loading', ready: (page) => page.getByRole('region', { name: 'Overview', busy: true }) },
  {
    workspace: 'home',
    state: 'fresh',
    ready: (page) => page.getByRole('link', { name: 'Continue in browser' }),
    assert: async (page, viewport) => {
      if (viewport.name !== 'desktop') return;
      const prompt = page.locator('[data-visual="setup-prompt"]');
      await assertMaxWidth(prompt, 560);
      const [card, title] = await Promise.all([prompt.boundingBox(), heading('Dashboard')(page).boundingBox()]);
      if (!card || !title || card.x !== title.x) throw new Error('Setup prompt and dashboard title must share a left edge');
    },
  },
  { workspace: 'home', state: 'syncing', ready: (page) => page.getByRole('progressbar', { name: /History \d+% synced/ }) },
  { workspace: 'home', state: 'populated', ready: (page) => page.getByRole('region', { name: 'Overview' }) },
  {
    workspace: 'dashboard-continuation',
    state: 'waiting-for-messages',
    act: (page) => page.getByRole('button', { name: 'Not now', exact: true }).click(),
    ready: (page) => page.getByRole('heading', { name: 'Waiting for messages', exact: true }),
    assert: async (page) => {
      if (await page.getByRole('heading', { name: 'Add older conversations', exact: true }).count()) {
        throw new Error('Dismissed message history prompt remained visible');
      }
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'allow-message-collection',
    ready: (page) => page.getByRole('heading', { name: 'Allow OnlyFans activity collection', exact: true }),
    assert: async (page) => {
      await page.getByRole('button', { name: 'Review in browser', exact: true }).waitFor({ state: 'visible' });
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'allow-onlyfans-access',
    ready: (page) => page.getByRole('heading', { name: 'Allow site access', exact: true }),
    assert: async (page) => {
      await page.getByRole('button', { name: 'Review in browser', exact: true }).waitFor({ state: 'visible' });
      await page.getByRole('region', { name: 'Overview' }).waitFor({ state: 'visible' });
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'message-collection-off',
    ready: (page) => page.getByRole('heading', { name: 'OnlyFans activity collection is off', exact: true }),
    assert: async (page) => {
      await page.getByRole('button', { name: 'Review in browser', exact: true }).waitFor({ state: 'visible' });
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'message-collection-paused',
    ready: (page) => page.getByRole('heading', { name: 'Paused by you', exact: true }),
    assert: async (page) => {
      await page.getByRole('button', { name: 'Review in browser', exact: true }).waitFor({ state: 'visible' });
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'add-older-conversations',
    ready: (page) => page.getByRole('heading', { name: 'Add older conversations', exact: true }),
    assert: async (page) => {
      await page.getByRole('button', { name: 'Review message history', exact: true }).waitFor({ state: 'visible' });
    },
  },
  {
    workspace: 'dashboard-continuation',
    state: 'add-older-conversations',
    variant: 'review-dialog',
    act: (page) => page.getByRole('button', { name: 'Review message history', exact: true }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Add older conversations' }).getByRole('button', { name: 'Allow message history', exact: true }),
  },
  {
    workspace: 'dashboard-continuation',
    state: 'activating-full-analytics',
    ready: text('Checking your activation…'),
  },
  {
    workspace: 'dashboard-continuation',
    state: 'full-analytics-unavailable',
    ready: text("New messages aren't being analyzed"),
  },
  {
    workspace: 'dashboard-continuation',
    state: 'activation-unavailable',
    ready: (page) => page.getByRole('heading', { name: 'Full analytics isn’t on', exact: true }),
    assert: (page) => text('Activation couldn’t finish.')(page).waitFor({ state: 'visible' }),
  },
  {
    workspace: 'analytics',
    state: 'loading',
    ready: (page) => page.getByRole('main').getByRole('status').first(),
    assert: assertLoadingGeometry,
  },
  { workspace: 'analytics', state: 'building', ready: text('Updating your analytics') },
  { workspace: 'analytics', state: 'unavailable', ready: text('Analytics are unavailable') },
  { workspace: 'analytics', state: 'baseline', ready: text('Early estimates') },
  {
    workspace: 'analytics',
    state: 'model',
    ready: (page) => page.getByRole('region', { name: 'Your replies' }),
    assert: async (page, viewport) => {
      await assertMetricHierarchy(page);
      if (viewport.name === 'desktop') {
        const replies = page.getByRole('region', { name: 'Your replies' });
        const box = await replies.boundingBox();
        if (!box || box.width < 280) throw new Error('Your replies panel became too narrow');
      }
    },
  },
  {
    workspace: 'analytics',
    state: 'error',
    ready: (page) => page.getByRole('main').getByRole('alert').getByRole('button', { name: 'Try again' }),
    assert: async (page, viewport) => {
      const state = page.locator('[data-visual="analytics-empty-state"]');
      await assertMaxWidth(state, 640);
      if (viewport.name === 'desktop') await assertCentered(state, page.getByRole('main'));
    },
  },
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({ workspace: 'inbox', state, ready: heading('Inbox') })),
  ...['loading', 'fresh', 'syncing', 'populated'].map((state) => ({
    workspace: 'settings',
    state,
    ready: (page) => state === 'loading' ? page.getByText('Loading settings…', { exact: true }).first() : page.getByRole('heading', { name: 'Stored messages' }),
    assert: async (page, viewport) => {
      if (viewport.name !== 'desktop') return;
      const frame = page.locator('[data-visual="settings-frame"]');
      await assertMaxWidth(frame, 880);
      await assertCentered(frame, page.getByRole('main'));
    },
  })),
  { workspace: 'passkey', state: 'resting', ready: heading('Confirm it’s you') },
  {
    workspace: 'passkey', state: 'cancelled',
    act: (page) => page.getByRole('button', { name: 'Sign in with passkey' }).click(),
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Sign-in didn’t finish.' }),
  },
  {
    workspace: 'analytics', state: 'model', variant: 'date-popover', viewports: ['desktop', 'narrow'],
    act: (page) => page.getByRole('button', { name: 'Change dates' }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Show messages from' }).getByLabel('Start date'),
  },
  {
    workspace: 'analytics', state: 'model', variant: 'tone-table', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Table', exact: true }).click(),
    ready: (page) => page.getByRole('table', { name: 'Message tone over time data' }),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'selected-conversation', viewports: ['narrow'],
    act: (page) => page.getByRole('list', { name: 'Conversation list' }).getByRole('button').first().click(),
    ready: (page) => page.getByRole('button', { name: 'Back to conversations' }),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'tablet-list', viewports: ['tablet'], modes: ['light'],
    ready: (page) => page.getByRole('list', { name: 'Conversation list' }),
  },
  {
    workspace: 'inbox', state: 'populated', variant: 'tablet-selected-conversation', viewports: ['tablet'], modes: ['light'],
    act: (page) => page.getByRole('list', { name: 'Conversation list' }).getByRole('button').first().click(),
    ready: (page) => page.getByRole('button', { name: 'Back to conversations' }),
  },
  {
    workspace: 'home', state: 'populated', variant: 'mobile-navigation-open', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Open navigation' }).click(),
    ready: (page) => page.locator('#mobile-navigation'),
  },
  {
    workspace: 'home', state: 'populated', variant: 'status-open', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: /Status: .*Show details/ }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Status details' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-review', viewports: ['narrow'], modes: ['light', 'dark'],
    act: page => openActivationReview(page),
    ready: page => page.getByRole('button', { name: 'Activate for free' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-waiting', viewports: ['narrow'], modes: ['light', 'dark'],
    act: async (page) => {
      await publishActivation(page, 'waiting');
      await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    },
    ready: page => page.getByText('Finish signing in in your browser.', { exact: true }),
    assert: async (page) => {
      if (await page.getByRole('button', { name: 'Activate for free' }).count()) throw new Error('Authentication must precede consent');
      if ((await page.evaluate(() => window.__workspaceFixture.desktopActions())).length) throw new Error('Returning from Chrome must not submit a decision');
    },
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-error', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await openActivationReview(page);
      await page.evaluate(() => window.__workspaceFixture.desktopActionError('reply_lost'));
      await page.getByRole('button', { name: 'Activate for free' }).click();
    },
    ready: page => page.getByRole('alert').filter({ hasText: 'Activation could not be confirmed.' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-provider-selection', viewports: ['narrow'], modes: ['light', 'dark'],
    act: page => publishActivation(page, 'selection'),
    ready: page => page.getByRole('group', { name: 'Sign-in options' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'pairing-comparison', viewports: ['narrow'], modes: ['light'],
    act: openPairingComparison,
    ready: (page) => page.getByRole('heading', { name: 'Confirm the connection', exact: true }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'history-consent', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await openPairingComparison(page);
      await page.getByRole('heading', { name: 'Confirm the connection', exact: true }).waitFor({ state: 'visible' });
      await page.getByRole('checkbox', { name: 'The codes match' }).check();
      await page.getByRole('button', { name: 'Confirm connection' }).click();
    },
    ready: (page) => page.getByRole('checkbox', { name: /I allow read-only syncing/ }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'archive-dialog', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Turn on archive' }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Turn on archive' }).getByRole('spinbutton', { name: 'Days to keep' }),
  },
  {
    workspace: 'settings', state: 'populated', variant: 'delete-confirmation', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Delete all messages' }).click(),
    ready: (page) => page.getByRole('dialog').filter({ hasText: 'Delete all messages?' }),
  },
  {
    // Pushed browser state and its controls stack under their text at narrow widths.
    workspace: 'settings', state: 'populated', variant: 'browser-controls', viewports: ['narrow'], modes: ['light', 'dark'],
    ready: text('New message collection is enabled.'),
  },
  {
    workspace: 'settings', state: 'populated', variant: 'linked-reconnecting', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.evaluate(async () => {
      const { bridgeTransportStore } = await import('/src/store/transportStore.ts');
      bridgeTransportStore.markDisconnected();
    }),
    ready: (page) => page.getByText('Extension linked to this app', { exact: true }),
  },
];
}
