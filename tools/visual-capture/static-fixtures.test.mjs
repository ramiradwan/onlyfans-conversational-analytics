import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { staticFixtures } from './static-fixtures.mjs';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const fixtures = await staticFixtures();

test('static capture covers the existing popup matrix, expanded disclosures, and all setup stages', () => {
  assert.equal(fixtures.filter((item) => item.surface === 'popup').length, 25);
  assert.equal(fixtures.filter((item) => item.surface === 'provisioning').length, 9);
  for (const fixture of fixtures) {
    const dom = new JSDOM(fixture.html);
    assert.equal(dom.window.document.querySelectorAll('script').length, 0);
    if (fixture.name === 'full_review') {
      assert(dom.window.document.querySelector('#full-disclosure details').open);
      assert(dom.window.document.querySelector('#preview-disclosure').classList.contains('hidden'));
      assert.equal(dom.window.document.querySelector('#full-secondary').textContent, 'Keep Preview');
    }
    dom.window.close();
  }
});

test('setup fixtures use real controller outcomes rather than invented status copy', () => {
  const expected = { connect: 0, confirm: 1, approve: 2, finish: 3, completed: -1 };
  for (const [name, step] of Object.entries(expected)) {
    const dom = new JSDOM(fixtures.find((item) => item.surface === 'provisioning' && item.name === name).html);
    const steps = [...dom.window.document.querySelectorAll('[data-step]')];
    assert.equal(steps.findIndex((node) => node.dataset.state === 'current'), step);
    dom.window.close();
  }
});
