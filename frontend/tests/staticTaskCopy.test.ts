import { createHash } from 'node:crypto';
import fs from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const read = (file: string) => fs.readFileSync('../' + file, 'utf8').replace(/\r\n/g, '\n');
const parse = (file: string) => new DOMParser().parseFromString(read(file), 'text/html');
const baseline = JSON.parse(fs.readFileSync('tests/fixtures/protected-static-disclosures.json', 'utf8'));

describe('task-focused static copy', () => {
  it('keeps protected disclosures unchanged except the requested sentence punctuation', () => {
    const html = read('extension/popup.html');
    for (const item of Object.values(baseline.blocks) as { start: string; end: string; sha256: string }[]) {
      // The owner explicitly removed semicolons from UI copy. No other disclosure edit is allowed.
      const block = html.slice(html.indexOf(item.start), html.indexOf(item.end))
        .replace('activate this Extension data handling. It is', 'activate this Extension data handling; it is');
      expect(createHash('sha256').update(block).digest('hex')).toBe(item.sha256);
    }
  });
  it.each(['extension/popup.html', 'app/provisioning/provisioning.html'])('has no semicolons in rendered copy in %s', (file) => {
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
