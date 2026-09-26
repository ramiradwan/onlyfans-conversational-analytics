import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = process.argv[2];
if (!output) throw new Error('Provide a new output directory.');
await mkdir(output, { recursive: false });
const browser = await chromium.launch({ headless: true });
const results = [];
try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, timezoneId: 'Europe/Helsinki' });
    await page.clock.setFixedTime(new Date('2026-09-19T12:00:00Z'));
    let mode = 'rows';
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      let json;
      if (url.pathname === '/api/v1/insights/questions' && method === 'GET') {
        json = { questions: [{ question: 'no_later_creator_reply.v1', enabled: true },
          { question: 'pricing_discussions.v1', enabled: false, reason: 'analytics_pricing_not_qualified' }] };
      } else if (url.pathname === '/api/v1/insights/questions' && method === 'POST') {
        assert.equal(route.request().headers()['x-csrf-token'], 'synthetic-question-csrf');
        const input = route.request().postDataJSON();
        assert.equal('account' in input || 'creator_account_id' in input, false);
        json = await page.evaluate(async (p) => (await import('/tests/questionFixture.ts')).answer(p, p.cursor ? 2 : 1), input);
        json.page.total_matching_conversations = 2;
        json.page.has_more = !input.cursor;
        json.next_cursor = input.cursor ? null : 'synthetic-next';
        if (mode === 'unknown') {
          json.page.rows = []; json.page.total_matching_conversations = 0;
          json.page.evaluated_conversation_count = 2; json.page.undetermined_conversation_count = 2;
          json.page.has_more = false; json.next_cursor = null;
        }
      } else if (url.pathname === '/api/v1/insights/questions/evidence') {
        json = await page.evaluate(async () => (await import('/tests/questionFixture.ts')).evidence());
        json.reference = route.request().postDataJSON();
      } else if (url.pathname.includes('/conversations/')) {
        json = await page.evaluate(async () => (await import('/tests/questionFixture.ts')).thread());
      } else { await route.abort(); return; }
      await route.fulfill({ json, headers: { 'Cache-Control': 'no-store' } });
    });
    await page.goto('http://127.0.0.1:5187/tests/question-browser.html');
    const run = page.getByRole('button', { name: 'Run question' });
    await run.focus(); await run.press('Enter');
    await page.getByRole('table').waitFor();
    await page.screenshot({ path: path.join(output, `questions-${width}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.getByRole('button', { name: 'Next page' }).click();
    await page.getByText('Page 2', { exact: true }).waitFor();
    await page.getByRole('button', { name: /View source 1/ }).click();
    await page.getByRole('dialog').getByText('Synthetic <img src=x onerror=alert(1)>', { exact: true }).waitFor();
    assert.equal(await page.getByRole('dialog').locator('img').count(), 0);
    await page.getByRole('button', { name: 'Open conversation', exact: true }).click();
    await page.getByText('Synthetic saved conversation', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, `source-${width}.png`), fullPage: true });
    const axeSource = await readFile(path.join(root, 'frontend/node_modules/axe-core/axe.min.js'), 'utf8');
    await page.addScriptTag({ content: axeSource });
    const audit = await page.evaluate(() => globalThis.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa'] }));
    assert.deepEqual(audit.violations.map((v) => v.id), []);
    await page.evaluate(() => window.dispatchEvent(new Event('synthetic-question-change')));
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.getByRole('table').count(), 0);
    mode = 'unknown';
    await page.reload();
    await run.click();
    await page.getByText(/Reply status could not be determined/).waitFor();
    await page.screenshot({ path: path.join(output, `undetermined-${width}.png`), fullPage: true });
    results.push({ width, pagination: true, source: true, conversation: true,
      changedSourceCleared: true, noPageOverflow: true, accessibilityViolations: audit.violations.length });
    await page.close();
  }
  await writeFile(path.join(output, 'report.json'), JSON.stringify({ synthetic: true, results }, null, 2));
  console.log(JSON.stringify(results));
} finally {
  await browser.close();
}
