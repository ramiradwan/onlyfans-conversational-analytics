import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const EXTENSION = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PRODUCT = path.resolve(EXTENSION, '..');
const PROTOCOL_TERMS = /CapabilityLicense|activation package|reissue package|seat[_ ]id|license[_ ]id|issuance[_ ]id|\bJWS\b|proof challenge|installation key JKT|commercial exchange ID/iu;

test('normal customer activation surfaces do not expose commercial protocol fields', async () => {
  const surfaces = await Promise.all([
    readFile(path.join(EXTENSION, 'popup.html'), 'utf8'),
    readFile(path.join(EXTENSION, 'runtime/customer-journey.mjs'), 'utf8'),
    readFile(path.join(PRODUCT, 'app/provisioning/provisioning.html'), 'utf8'),
    readFile(path.join(PRODUCT, 'frontend/src/views/SettingsWithVaultView.tsx'), 'utf8'),
  ]);

  assert.doesNotMatch(surfaces.join('\n'), PROTOCOL_TERMS);
});
