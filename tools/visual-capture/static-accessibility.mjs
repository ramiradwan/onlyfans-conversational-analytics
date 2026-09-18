import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const axeSource = await readFile(new URL('../../frontend/node_modules/axe-core/axe.min.js', import.meta.url), 'utf8');

export async function assertStaticAccessibility(page) {
  await page.addScriptTag({ content: axeSource });
  const violations = await page.evaluate(async () => {
    const result = await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] } });
    return result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
      .map((item) => ({ id: item.id, impact: item.impact, targets: item.nodes.map((node) => node.target) }));
  });
  assert.deepEqual(violations, [], 'Static accessibility violations');
}
