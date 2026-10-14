import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

test('the local browser page can navigate only to the setup resource, which cannot be embedded', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.web_accessible_resources, [{ resources: ['setup.html'], matches: ['http://bridge.localhost/*'] }]);
  assert.match(manifest.content_security_policy.extension_pages, /frame-ancestors 'none';/);
});
