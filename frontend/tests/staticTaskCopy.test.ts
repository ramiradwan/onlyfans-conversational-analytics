import { createHash } from 'node:crypto';
import fs from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const read = (file: string) => fs.readFileSync('../' + file, 'utf8').replace(/\r\n/g, '\n');
const parse = (file: string) => new DOMParser().parseFromString(read(file), 'text/html');
const baseline = JSON.parse(fs.readFileSync('tests/fixtures/protected-static-disclosures.json', 'utf8'));

describe('task-focused static copy', () => {
  it('keeps protected disclosures unchanged', () => {
    const doc = parse('extension/setup.html');
    for (const [id, item] of Object.entries(baseline.blocks) as [string, { element_sha256: string }][]) {
      const element = doc.getElementById(id);
      expect(element, id).not.toBeNull();
      const disclosure = element!.cloneNode(true) as HTMLElement;
      // The approved interaction keeps submission enabled to explain missing
      // checkboxes. Preserve the original disclosure bytes/hash while allowing
      // only that button's initial interactive state to differ.
      if (id === 'pre-mode') {
        expect(element!.querySelector('#activate-software')?.hasAttribute('disabled')).toBe(false);
        // The task shell and ordinary action copy may change. The original
        // acknowledgement markup is pinned independently of those elements.
        const acknowledgements = read('extension/setup.html').match(/<label class="check-row">[\s\S]*?<\/label>/g);
        expect(acknowledgements).toHaveLength(2);
        expect(createHash('sha256').update(acknowledgements!.join('')).digest('hex')).toBe(baseline.acknowledgements_sha256);
        continue;
      }
      expect(createHash('sha256').update(disclosure.outerHTML).digest('hex')).toBe(item.element_sha256);
    }
  });
  it.each(['extension/popup.html', 'extension/setup.html', 'extension/options.html', 'frontend/onboarding/index.html'])('has no semicolons in rendered copy in %s', (file) => {
    const doc = parse(file);
    doc.querySelectorAll('style,script').forEach((node) => node.remove());
    expect(doc.body.textContent).not.toContain(';');
  });
  it.each(['extension/popup.js', 'extension/runtime/customer-journey.mjs', 'frontend/onboarding/view.js'])('has no semicolons in operational messages in %s', (file) => {
    const source = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    const visit = (node: ts.Node) => {
      if (ts.isStringLiteralLike(node)) expect(node.text, file).not.toMatch(/[A-Za-z];\s/);
      ts.forEachChild(node, visit);
    };
    visit(source);
  });
  it('provides one setup instruction per task and keeps feedback empty until needed', () => {
    const doc = parse('frontend/onboarding/index.html');
    expect(doc.querySelector('#task-status')?.textContent).toBe('');
    expect(doc.querySelector('#task-title')?.textContent).toBe('Loading setup');
    expect(doc.querySelector('#recovery-options')).toBeNull();
    for (const control of doc.querySelectorAll('[aria-describedby]')) {
      for (const id of control.getAttribute('aria-describedby')!.split(' ')) expect(doc.getElementById(id), id).not.toBeNull();
    }
  });
});
