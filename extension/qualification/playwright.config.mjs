import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'release.spec.mjs',
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [['list']],
  outputDir: process.env.OFCA_BROWSER_OUTPUT ?? 'test-results/release',
});
