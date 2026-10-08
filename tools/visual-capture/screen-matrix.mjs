export const FIXED_NOW = '2026-06-30T12:05:00.000Z';
export const MODES = ['light', 'dark'];
export const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'narrow', width: 390, height: 844 },
  { name: 'tablet', width: 820, height: 900, targetedOnly: true },
];

const heading = (name) => (page) => page.getByRole('heading', { level: 1, name });
const text = (value) => (page) => page.getByText(value, { exact: true }).first();

/** Each screen names the locator that proves its state rendered before capture. */
export function createScreens({ assertMaxWidth, assertCentered, assertLoadingGeometry, assertMetricHierarchy } = {}) {
return [
  ...['current', 'pending', 'unavailable', 'degraded'].map((state) => ({ workspace: 'graph', state,
    ready: heading('Graph explorer'), act: (page) => page.evaluate((status) => window.__workspaceFixture.projection(status), state) })),
  { workspace: 'home', state: 'loading', ready: text('Loading dashboard…') },
  {
    workspace: 'home',
    state: 'fresh',
    ready: (page) => page.getByRole('link', { name: 'Continue setup' }),
    assert: async (page, viewport) => {
      if (viewport.name !== 'desktop') return;
      const prompt = page.locator('[data-visual="setup-prompt"]');
      await assertMaxWidth(prompt, 560);
      await assertCentered(prompt, page.getByRole('main'));
    },
  },
  { workspace: 'home', state: 'syncing', ready: (page) => page.getByRole('progressbar', { name: /History \d+% synced/ }) },
  { workspace: 'home', state: 'populated', ready: (page) => page.getByRole('region', { name: 'Overview' }) },
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
  { workspace: 'passkey', state: 'resting', ready: heading('Protect access to your messages') },
  {
    workspace: 'passkey', state: 'cancelled',
    act: (page) => page.getByRole('button', { name: 'Sign in with passkey' }).click(),
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Sign-in did not finish.' }),
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
    workspace: 'settings', state: 'fresh', variant: 'activation-dialog', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Turn on full analytics' }).click(),
    ready: (page) => page.getByRole('dialog', { name: 'Turn on full analytics' }).getByRole('link', { name: 'Open secure setup' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-return', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Turn on full analytics' }).click();
      await page.getByRole('link', { name: 'Open secure setup' }).dispatchEvent('click');
    },
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Secure setup opened.' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'activation-error', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Turn on full analytics' }).click();
      await page.getByRole('textbox', { name: 'Activation code' }).fill('invalid');
      await page.getByRole('button', { name: 'Activate', exact: true }).click();
    },
    ready: (page) => page.getByRole('alert').filter({ hasText: 'Enter the full activation code and try again.' }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'pairing-comparison', viewports: ['narrow'], modes: ['light'],
    act: (page) => page.getByRole('button', { name: 'Connect extension' }).click(),
    ready: (page) => page.getByText('Check the code', { exact: true }),
  },
  {
    workspace: 'settings', state: 'fresh', variant: 'history-consent', viewports: ['narrow'], modes: ['light'],
    act: async (page) => {
      await page.getByRole('button', { name: 'Connect extension' }).click();
      await page.getByText('Check the code', { exact: true }).waitFor({ state: 'visible' });
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
    ready: text('Collecting in the browser.'),
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
