import { defineConfig } from 'vitest/config';

import { aliasEntries } from './scripts/aliases.config.js';

export default defineConfig({
  resolve: {
    alias: aliasEntries,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    // Headroom above the 5s default for jsdom environment setup under parallel load.
    testTimeout: 15000,
  },
});
