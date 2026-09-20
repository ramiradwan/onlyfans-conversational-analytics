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
      expect(createHash('sha256').update(element!.outerHTML).digest('hex')).toBe(item.element_sha256);
    }
  });
  it.each(['extension/popup.html', 'extension/setup.html', 'extension/options.html', 'app/provisioning/provisioning.html'])('has no semicolons in rendered copy in %s', (file) => {
    const doc = parse(file);
    doc.querySelectorAll('style,script').forEach((node) => node.remove());
    expect(doc.body.textContent).not.toContain(';');
  });
  it.each(['extension/popup.js', 'extension/runtime/customer-journey.mjs', 'app/provisioning/provisioning.js'])('has no semicolons in operational messages in %s', (file) => {
    const source = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    const visit = (node: ts.Node) => {
      if (ts.isStringLiteralLike(node)) expect(node.text, file).not.toMatch(/[A-Za-z];\s/);
      ts.forEachChild(node, visit);
    };
    visit(source);
  });
  it('provides one setup instruction per task and keeps feedback empty until needed', () => {
    const doc = parse('app/provisioning/provisioning.html');
    expect(doc.querySelector('#provisioning-status')?.textContent).toBe('');
    expect(doc.querySelector('.page-header')?.textContent).not.toMatch(/Four short|Current step/);
    expect(doc.querySelector('#binding-step')?.textContent).not.toMatch(/Creator approval|contact support|Check again after/);
    expect(doc.querySelector('#creator-approval-unavailable')?.textContent).toContain('setup tab where you got your code');
    expect(doc.querySelector('.recovery-help')?.textContent).toContain('browser history');
    for (const selector of ['#claim-action-help', '#identity-confirm-help', '#binding-action-help', '#finalize-action-help']) {
      expect(doc.querySelector(selector)).toBeNull();
    }
    for (const control of doc.querySelectorAll('[aria-describedby]')) {
      for (const id of control.getAttribute('aria-describedby')!.split(' ')) expect(doc.getElementById(id), id).not.toBeNull();
    }
  });
});
