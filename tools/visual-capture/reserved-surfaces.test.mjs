import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
for (const surface of ['popup', 'setup', 'options']) {
  test(`${surface} retains feedback and content frames from its first document`, async () => {
    const html = await readFile(new URL(`../../extension/${surface}.html`, import.meta.url), 'utf8');
    const dom = new JSDOM(html);
    try {
      const document = dom.window.document;
      assert(document.querySelector('[data-reserved-region]'));
      assert(document.querySelector('#feedback').closest('[data-reserved-region]'));
      assert.equal(document.querySelector('[data-reserved-region].hidden'), null);
      if (surface === 'popup') assert(document.querySelector('[data-reserved-region="popup-status"] [data-region-content]'));
      if (surface === 'options') assert(document.querySelector('#connection-details-dialog'));
    } finally { dom.window.close(); }
  });
}

test('native recovery needs no package input and keeps setup-code receiving separate', async () => {
  const html = await readFile(new URL('../../frontend/onboarding/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html);
  try {
    const document = dom.window.document;
    assert.equal(document.querySelectorAll('[data-step]').length, 3);
    assert.equal(document.querySelector('#recovery-package'), null);
    assert.equal(document.querySelector('textarea'), null);
    assert.equal(document.querySelector('#task-status').getAttribute('role'), 'status');
    assert.equal(document.querySelector('#task-action').getAttribute('aria-describedby'), 'task-description task-status');
    assert.equal(document.querySelector('#setup-code-open').textContent, 'I have a setup code');
    assert.equal(document.querySelector('#setup-code-open').hidden, true);
  } finally { dom.window.close(); }
});
