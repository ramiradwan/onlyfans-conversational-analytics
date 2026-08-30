import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const html = await readFile(new URL('../popup.html', import.meta.url), 'utf8');
const script = await readFile(new URL('../popup.js', import.meta.url), 'utf8');

function requireFragment(pattern, label) {
  const match = html.match(pattern);
  assert.ok(match, `${label} must exist`);
  return match[0];
}

test('Full disclosure uses Legal-approved ordering and Enable Full analytics action', () => {
  const fullDisclosure = requireFragment(
    /<div id="full-disclosure"[\s\S]*?<\/div>/,
    'Full disclosure',
  );
  const ordered = [
    'Message content:',
    'Same-computer transfer:',
    'Local storage and retention:',
    'Deletion boundary:',
    'Other people’s information:',
    'Extension Privacy Notice',
  ];
  let cursor = -1;
  for (const text of ordered) {
    const next = fullDisclosure.indexOf(text);
    assert.ok(next > cursor, `${text} must appear in approved order`);
    cursor = next;
  }
  assert.match(fullDisclosure, />Enable Full analytics<\/button>/);
  assert.match(fullDisclosure, /id="full-secondary"/);
  assert.doesNotMatch(fullDisclosure, /Connect full analytics/);
  assert.doesNotMatch(script, /transition\(['"]full['"]\)/);
  assert.match(script, /LEGAL_CHOOSE_MODE_MESSAGE_TYPE/);
});

test('Activate Software is separate from mode choice and no UI calls it consent', () => {
  const preMode = requireFragment(
    /<section id="pre-mode"[\s\S]*?<\/section>/,
    'Pre-mode activation section',
  );
  assert.match(
    preMode,
    /<button[^>]*id="activate-software"[^>]*>\s*Activate Software\s*<\/button>/,
  );
  assert.match(preMode, /does not enable Full analytics/);
  assert.doesNotMatch(html, /GDPR consent/i);
  assert.doesNotMatch(script, /Full consent saved/);
});

test('vendored schema bytes remain exactly pinned to Legal v2', async () => {
  const bytes = await readFile(new URL('../../shared/legal/activation-evidence.schema.json', import.meta.url));
  const lock = JSON.parse(await readFile(
    new URL('../../shared/legal/activation-evidence.lock.json', import.meta.url),
    'utf8',
  ));
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const gitBlob = createHash('sha1')
    .update(Buffer.from(`blob ${bytes.length}\0`))
    .update(bytes)
    .digest('hex');
  assert.equal(sha256, lock.source_sha256);
  assert.equal(gitBlob, lock.source_blob_sha);
  assert.equal(lock.schema_version, '2.0');
});
