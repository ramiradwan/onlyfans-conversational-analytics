import assert from 'node:assert/strict';
import test from 'node:test';

import { validateCustomerReleaseConfig } from '../runtime/customer-release-config.mjs';

const base = {
  schema: 'ofca-customer-release/v1',
  desktop_app_download_url: '',
};

test('development may omit the desktop download while production packaging may not', () => {
  assert.deepEqual(validateCustomerReleaseConfig(base), base);
  assert.throws(
    () => validateCustomerReleaseConfig(base, { requireDesktopDownload: true }),
    /desktop_app_download_url_required/,
  );
});

test('customer desktop download accepts only credential-free HTTPS', () => {
  assert.equal(validateCustomerReleaseConfig({
    ...base,
    desktop_app_download_url: 'https://downloads.example.test/latest',
  }, { requireDesktopDownload: true }).desktop_app_download_url, 'https://downloads.example.test/latest');

  for (const desktop_app_download_url of [
    'http://downloads.example.test/latest',
    'https://user:pass@downloads.example.test/latest',
    'https://downloads.invalid/latest',
  ]) {
    assert.throws(() => validateCustomerReleaseConfig({
      ...base, desktop_app_download_url,
    }, { requireDesktopDownload: true }));
  }
});

test('customer release configuration rejects extra or malformed fields', () => {
  assert.throws(() => validateCustomerReleaseConfig({ ...base, extra: true }));
  assert.throws(() => validateCustomerReleaseConfig({
    schema: 'ofca-customer-release/v2',
    desktop_app_download_url: '',
  }));
});
