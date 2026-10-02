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

test('the setup code is a single line and the four panes share one fixed stage', async () => {
  const html = await readFile(new URL('../../app/provisioning/provisioning.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html);
  try {
    const document = dom.window.document;
    assert.equal(document.querySelector('#claim-package').tagName, 'INPUT');
    assert.equal(document.querySelectorAll('[data-reserved-region="provisioning-stage"] .step').length, 4);
    assert.equal(document.querySelector('details'), null);
    const styles = [...document.querySelectorAll('style')].map((style) => style.textContent).join('\n');
    assert.match(styles, /stage-enter 200ms/);
    assert.match(styles, /stage-breathe 320ms/);
    assert.match(styles, /opacity 320ms/);
    assert.match(styles, /prefers-reduced-motion: reduce[\s\S]*animation:none/);
  } finally { dom.window.close(); }
});
