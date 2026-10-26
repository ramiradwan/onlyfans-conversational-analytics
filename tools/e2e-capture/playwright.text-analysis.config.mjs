import configuration from './playwright.config.mjs';

if (process.env.BROWSER_CI_PHASE) {
  throw new Error('Targeted text analysis runs cannot produce browser lane evidence.');
}

export default {
  ...configuration,
  globalSetup: undefined,
  testMatch: [
    '**/text-analysis-processing.spec.mjs',
    '**/text-analysis-preview.spec.mjs',
    '**/text-analysis-recovery.spec.mjs',
  ],
};
