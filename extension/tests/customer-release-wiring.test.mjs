import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const popup = await readFile(new URL('../ui/surface-client.mjs', import.meta.url), 'utf8');
const packageDocument = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));

test('extension desktop installer action is sourced from release-owned customer config', () => {
  assert.match(popup, /import \{ customerReleaseConfig \} from ['"]\.\.\/runtime\/customer-release-config\.mjs['"]/);
  assert.match(popup, /desktop_app_download_url:\s*customerReleaseConfig\.desktop_app_download_url/);
  assert.doesNotMatch(popup, /candidate\.desktop_app_download_url/);
});

test('Chrome packaging runs the customer release gate before building the package', () => {
  assert.equal(
    packageDocument.scripts['package:chrome'],
    'npm run verify:customer-release && node build.mjs --package',
  );
  assert.equal(
    packageDocument.scripts['verify:customer-release'],
    'node qualification/customer-release-gate.mjs',
  );
});
