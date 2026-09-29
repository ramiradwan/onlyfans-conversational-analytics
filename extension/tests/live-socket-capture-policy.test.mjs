import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { captureIsEnabled } from '../transport/read-only-capture-ingestion.mjs';

const capturePolicy = JSON.parse(readFileSync(
  new URL('../../shared/fixtures/protocol/live-socket-capture-policy.json', import.meta.url), 'utf8',
));

test("served policy admits only the platform's live socket path and its bare path", () => {
  for (const [path, expected] of [
    ['/ws3/17', true], ['/ws3', true], ['/ws3/17/x', false], ['/ws3x', false],
  ]) {
    assert.equal(captureIsEnabled({ capture_policy: capturePolicy }, 'messages', path), expected, path);
  }
});

test("socket fixtures exercise the platform's live socket path", () => {
  for (const relative of [
    '../../tools/e2e-capture/fixtures/synthetic-platform.mjs', '../qa/qa-helper-page.js',
  ]) {
    const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
    const url = source.match(/wss:\/\/[^'"\s]+/)[0];
    assert.match(new URL(url).pathname, /^\/ws3\/\d{2}$/);
  }
});
