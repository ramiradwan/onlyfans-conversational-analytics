import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { auditActivationDisclosures } from '../qualification/legal-binding-literals.mjs';

const html = await readFile(new URL('../setup.html', import.meta.url), 'utf8');
const script = (await Promise.all(['../setup.js', '../ui/actions.mjs'].map((file) => readFile(new URL(file, import.meta.url), 'utf8')))).join('\n');

function requireFragment(pattern, label) {
  const match = html.match(pattern);
  assert.ok(match, `${label} must exist`);
  return match[0];
}

const modeChoice = requireFragment(/<section id="mode-choice"[\s\S]*?<\/section>/, 'Mode choice');

// Exact adopted customer-render sections; UI containers/actions stay outside these bytes.
const APPROVED_SECTION_HASHES = {
  preview: '92642f8a5c46c5026de0b6845dbfdb0c4adbedaa5612d2d634c4d796a02f4fa2',
  full: '5527bb2a4b92d4a3381c77ccbd944ac73379ec4f2b1d01c73e6538b9f7a38151',
  'separate-choices': 'd9b04f4a0f4e015e99092f3d89af1cce0af430fb823b4d97e10a497cbc833837',
};

function approvedSection(name) {
  const matches = [...html.matchAll(new RegExp(`data-approved-copy="${name}">\\n([\\s\\S]*?)</div>`, 'g'))];
  assert.equal(matches.length, 1, `${name} must have one complete section`);
  assert.equal(createHash('sha256').update(matches[0][1]).digest('hex'), APPROVED_SECTION_HASHES[name]);
  return matches[0][1];
}

test('Preview disclosure retains the adopted section and exact actions', () => {
  approvedSection('preview');
  const preview = requireFragment(/<div id="preview-disclosure"[\s\S]*?(?=<div id="full-disclosure")/, 'Preview disclosure');
  assert.match(preview, /id="enable-preview"[^>]*>Enable Preview<\/button>/);
  assert.match(preview, /id="not-now-preview"[^>]*>Not now<\/button>/);
  assert.match(preview, /id="review-full"[^>]*>Review Full analytics<\/button>/);
});

test('Full disclosure retains the adopted section and separate Full action', () => {
  approvedSection('full');
  const full = requireFragment(/<div id="full-disclosure"[\s\S]*?(?=<div id="separate-choices-disclosure")/, 'Full disclosure');
  assert.match(full, /id="enable-full"[^>]*>Enable Full analytics<\/button>/);
  assert.match(full, /id="full-secondary"[^>]*>Not now<\/button>/);
  assert.match(script, /'Keep Preview' : 'Not now'/);
  assert.doesNotMatch(script, /transition\(['"]full['"]\)/);
  assert.match(script, /LEGAL_CHOOSE_MODE_MESSAGE_TYPE/);
});

test('the distinct separate-choices section retains its adopted bytes', () => {
  approvedSection('separate-choices');
});

test('the package copy audit refuses changed, missing and duplicate disclosures', () => {
  auditActivationDisclosures(html);
  assert.throws(() => auditActivationDisclosures(html.replace('local tokens', 'local counts')));
  assert.throws(() => auditActivationDisclosures(html.replace('data-approved-copy="full"', 'data-removed-copy="full"')));
  assert.throws(() => auditActivationDisclosures(`${html}<div data-approved-copy="preview">\n${approvedSection('preview')}</div>`));
});

test('mode choice keeps every required point visible without a collapsed section', () => {
  assert.doesNotMatch(modeChoice, /<details|<summary/);
  assert.doesNotMatch(modeChoice, /\shidden[\s=>]/);
  const hiddenByDefault = [...modeChoice.matchAll(/<[^>]*\bclass="[^"]*\bhidden\b[^"]*"[^>]*>/g)]
    .map(([tag]) => tag.match(/id="([^"]+)"/)?.[1]);
  assert.deepEqual(hiddenByDefault, ['mode-choice', 'full-disclosure']);
});

test('combined mode review retains separate software activation and consent commands', () => {
  const preMode = requireFragment(
    /<section id="pre-mode"[\s\S]*?<\/section>/,
    'Pre-mode activation section',
  );
  assert.match(
    preMode,
    /<button[^>]*id="activate-software"[^>]*>\s*Continue\s*<\/button>/,
  );
  assert.match(script, /type: LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE/);
  assert.match(script, /type: LEGAL_CHOOSE_MODE_MESSAGE_TYPE/);
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
