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
const findingsDialog = page => page.getByRole('dialog', { name: 'All topics', exact: true });
const findingsTrigger = page => page.getByRole('button', { name: 'See all topics', exact: true });

async function openFindings(page) {
  await page.route('**/src/story-only/findingsFixtures.ts*', async route => {
    const response = await route.fetch();
    const source = await response.text();
    if (!source.includes('Example topic')) throw new Error('Synthetic findings label is missing');
    await route.fulfill({ response, body: source.replace('Example topic', 'Pricing questions') });
  });
  const url = new URL(page.url());
  url.searchParams.delete('workspace');
  url.searchParams.set('stability', 'findings');
  if (page.viewportSize().width === 320) {
    await page.addInitScript(() => {
      const root = new MutationObserver(() => {
        if (!document.documentElement) return;
        document.documentElement.style.fontSize = '200%';
        root.disconnect();
      });
      root.observe(document, { childList: true, subtree: true });
    });
  }
  await page.goto(url.href, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
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

/** Each screen names the locator that proves its state rendered before capture. */
export function createScreens({ assertMaxWidth, assertCentered, assertLoadingGeometry, assertMetricHierarchy } = {}) {
return [
  {
    workspace: 'findings', state: 'first-insights', viewports: ['desktop', 'narrow', 'narrow-2x'],
    act: openFindings,
    ready: page => page.getByRole('heading', { name: findingHeadline, exact: true }),
    assert: async (page, viewport) => {
      await assertFindingTitle(page.getByRole('heading', { name: findingHeadline, exact: true }), viewport);
      const bar = await page.locator('[data-visual="finding-bar"]').first().evaluate(node => ({
        animation: getComputedStyle(node).animationName,
        fill: node.getBoundingClientRect().width / node.parentElement.getBoundingClientRect().width,
      }));
      if (bar.animation !== 'none' || Math.abs(bar.fill - 0.5) > 0.005) throw new Error('Finding bar missed its reduced-motion end state');
    },
  },
  {
    workspace: 'findings', state: 'all-topics', viewports: ['desktop', 'narrow', 'narrow-2x'],
    act: async page => {
      await openFindings(page);
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
    ready: (page) => page.getByRole('heading', { name: 'Allow message collection', exact: true }),
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
    ready: (page) => page.getByRole('heading', { name: 'Message collection is off', exact: true }),
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
