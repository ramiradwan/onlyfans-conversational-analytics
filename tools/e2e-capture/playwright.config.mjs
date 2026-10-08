import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig } from '@playwright/test';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
// The added inventory pass emits closed identities only. Execution retains the
// original console reporter and its existing assertion-redaction boundary.
const reporters = process.env.BROWSER_CI_PHASE === 'inventory'
  ? [] : process.env.CI ? [['line']] : [['list']];
if (process.env.BROWSER_CI_PHASE) {
  reporters.push([path.join(ROOT, 'ci', 'reporter.mjs'), {
    mode: process.env.BROWSER_CI_PHASE,
    lane: process.env.BROWSER_CI_LANE,
    file: process.env.BROWSER_CI_EVENTS,
  }]);
}

export default defineConfig({
  testDir: './tests',
  globalSetup: path.join(ROOT, 'global-setup.mjs'),
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 180_000,
  expect: {
    timeout: 12_000,
  },
  reporter: reporters,
  outputDir: './test-results',
  use: {
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
  },
});
