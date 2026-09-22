import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const html = await readFile(new URL('../setup.html', import.meta.url), 'utf8');
const script = (await Promise.all(['../setup.js', '../ui/actions.mjs'].map((file) => readFile(new URL(file, import.meta.url), 'utf8')))).join('\n');

function requireFragment(pattern, label) {
  const match = html.match(pattern);
  assert.ok(match, `${label} must exist`);
  return match[0];
}

function listItems(fragment) {
  return [...fragment.matchAll(/<li>([\s\S]*?)<\/li>/g)]
    .map(([, item]) => item.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim());
}

const modeChoice = requireFragment(/<section id="mode-choice"[\s\S]*?<\/section>/, 'Mode choice');

// Legal's approved prominent-disclosure copy, rendered with the notice URL as a link.
const APPROVED_PREVIEW_POINTS = [
  'Transient inspection: generating Preview metrics can require the Extension to transiently inspect supported chat and message response information as the supported page handles it.',
  'Retained Preview data: Preview retains only identifier-free daily counts and message direction information.',
  'Seven-day retention: retained Preview metrics are automatically removed after seven days.',
  'No content or identifiers retained: Preview retains no message text or participant, message or chat identifiers.',
];
const APPROVED_FULL_POINTS = [
  'Message content: Full handles and retains message text and related conversation, message and participant information.',
  'Same-computer transfer: Full information leaves the Extension but remains on the same computer. The Extension sends it to the companion analytics service running on that computer.',
  'Persistence: the Extension and companion analytics service can both retain Full information at the same time, and there is currently no general automatic age-based expiry for the main Full conversation dataset.',
  'Deletion boundary: deleting Extension data does not delete Full information already retained by the companion analytics service.',
  'Other people: Full contains subscriber and other-person information. Enabling Full is not consent on behalf of a subscriber or another person.',
  'Extension Privacy Notice: read the complete data-handling description.',
];

test('Preview disclosure renders the Legal-approved points and actions', () => {
  const preview = requireFragment(/<div id="preview-disclosure"[\s\S]*?(?=<div id="full-disclosure")/, 'Preview disclosure');
  assert.deepEqual(listItems(preview), APPROVED_PREVIEW_POINTS);
  assert.match(preview, /Read the <a class="extension-privacy-link" href="#">Extension Privacy Notice<\/a>\./);
  assert.match(preview, /id="enable-preview"[^>]*>Enable Preview<\/button>/);
  assert.match(preview, /id="not-now-preview"[^>]*>Not now<\/button>/);
  assert.match(preview, /id="review-full"[^>]*>Review Full analytics<\/button>/);
});

test('Full disclosure uses Legal-approved ordering and Enable Full analytics action', () => {
  const fullDisclosure = requireFragment(/<div id="full-disclosure"[\s\S]*?(?=<\/section>)/, 'Full disclosure');
  assert.match(fullDisclosure, /Before choosing Full, review these points:/);
  assert.deepEqual(listItems(fullDisclosure), APPROVED_FULL_POINTS);
  assert.match(fullDisclosure, /class="extension-privacy-link"/);
  assert.match(fullDisclosure, />Enable Full analytics<\/button>/);
  assert.match(fullDisclosure, /id="full-secondary"/);
  assert.match(script, /'Keep Preview' : 'Not now'/);
  assert.doesNotMatch(fullDisclosure, /Connect full analytics/);
  assert.doesNotMatch(script, /transition\(['"]full['"]\)/);
  assert.match(script, /LEGAL_CHOOSE_MODE_MESSAGE_TYPE/);
});

test('mode choice keeps every required point visible without a collapsed section', () => {
  assert.doesNotMatch(modeChoice, /<details|<summary/);
  assert.doesNotMatch(modeChoice, /\shidden[\s=>]/);
  const hiddenByDefault = [...modeChoice.matchAll(/<[^>]*\bclass="[^"]*\bhidden\b[^"]*"[^>]*>/g)]
    .map(([tag]) => tag.match(/id="([^"]+)"/)?.[1]);
  assert.deepEqual(hiddenByDefault, ['mode-choice', 'full-disclosure']);
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
