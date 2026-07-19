import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 180_000,
  expect: {
    timeout: 12_000,
  },
  reporter: process.env.CI ? [['line']] : [['list']],
  outputDir: './test-results',
  use: {
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
  },
});
