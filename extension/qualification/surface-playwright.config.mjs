import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.',
  testMatch: ['customer-journey-visual.spec.mjs', 'surface-interactions.spec.mjs'],
  workers: 2,
  retries: 0,
  timeout: 20000,
  reporter: [['list']],
  outputDir: 'test-results/surfaces',
  use: { reducedMotion: 'reduce', locale: 'en-US',
    ...(process.env.OFCA_CHROMIUM ? { launchOptions: { executablePath: process.env.OFCA_CHROMIUM } } : {}),
  },
});
