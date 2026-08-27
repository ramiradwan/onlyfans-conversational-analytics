import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const manifestPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'manifest.json');
const expectedExtensionId = 'mldllkjpnnjhdccpofhebhlhigpefcba';

function extensionIdFromPublicKey(publicKeyBase64) {
  const publicKeyDer = crypto.createPublicKey({
    key: Buffer.from(publicKeyBase64, 'base64'),
    format: 'der',
    type: 'spki',
  }).export({ type: 'spki', format: 'der' });
  const digest = crypto.createHash('sha256').update(publicKeyDer).digest('hex').slice(0, 32);
  return [...digest].map((nibble) => String.fromCharCode(97 + Number.parseInt(nibble, 16))).join('');
}

test('manifest key produces the pinned extension ID', () => {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

  assert.equal(extensionIdFromPublicKey(manifest.key), expectedExtensionId);
});
