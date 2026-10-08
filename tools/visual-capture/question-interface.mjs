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
const heldResponses = new Set();
async function bounds(locator) {
  return locator.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => {})));
    const { x, y, width, height } = element.getBoundingClientRect();
    return { x, y, width, height };
  });
}
try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, timezoneId: 'Europe/Helsinki' });
    await page.clock.setFixedTime(new Date('2026-09-19T12:00:00Z'));
    let mode = 'rows';
    const delayed = new Map();
    const geometry = [];
    function hold(kind) {
      assert.equal(delayed.has(kind), false);
      let entered, rejectEntered, release;
      const wait = { entered: new Promise((resolve, reject) => { entered = resolve; rejectEntered = reject; }),
        released: new Promise((resolve) => { release = resolve; }),
        enter: () => { clearTimeout(timer); entered(); }, release: () => { clearTimeout(timer); release(); } };
      const timer = setTimeout(() => { rejectEntered(new Error(`${kind} request did not arrive`)); release(); }, 15_000);
      delayed.set(kind, wait);
      heldResponses.add(wait);
      return wait;
    }
    function unchanged(before, after, state) {
      for (const control of Object.keys(before)) {
        for (const axis of ['x', 'y', 'width', 'height']) {
          assert.ok(Math.abs(before[control][axis] - after[control][axis]) <= 0.75,
            `${width}px ${state}: ${control} ${axis} moves from ${before[control][axis]} to ${after[control][axis]}`);
        }
      }
      geometry.push({ state, before, after });
    }
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      let json, kind;
      let status = 200;
      if (url.pathname === '/api/v1/insights/questions' && method === 'GET') {
        kind = 'catalog';
        json = { questions: [{ question: 'no_later_creator_reply.v1', enabled: true },
          { question: 'pricing_discussions.v1', enabled: false, reason: 'analytics_pricing_not_qualified' }] };
      } else if (url.pathname === '/api/v1/insights/questions' && method === 'POST') {
        kind = 'answer';
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
        if (mode === 'error') {
          status = 503;
          json = { detail: { availability: 'error' } };
        }
        if (mode === 'stale') {
          status = 404;
          json = { detail: { code: 'analytics_question_cursor_stale' } };
        }
      } else if (url.pathname === '/api/v1/insights/questions/evidence') {
        kind = 'evidence';
        json = await page.evaluate(async () => (await import('/tests/questionFixture.ts')).evidence());
        json.reference = route.request().postDataJSON();
      } else if (url.pathname.includes('/conversations/')) {
        kind = 'history';
        json = await page.evaluate(async () => (await import('/tests/questionFixture.ts')).thread());
        json.items = Array.from({ length: 30 }, (_, index) => ({ ...json.items[0],
          message_id: `synthetic-message-${index}`, sent_at: `2026-09-19T10:${String(index).padStart(2, '0')}:00Z`,
          text: index === 0 ? json.items[0].text : `Synthetic saved context ${index}` }));
      } else { await route.abort(); return; }
      const wait = delayed.get(kind);
      if (wait) {
        delayed.delete(kind);
        wait.enter();
        await wait.released;
        heldResponses.delete(wait);
      }
      await route.fulfill({ status, json, headers: { 'Cache-Control': 'no-store' } });
    });
    const catalogWait = hold('catalog');
    await page.goto('http://127.0.0.1:5187/tests/question-browser.html');
    await catalogWait.entered;
    await page.evaluate(() => document.fonts.ready);
    const run = page.locator('button[type="submit"]');
    async function controls() {
      return { question: await bounds(page.getByRole('combobox', { name: 'Question' })),
        start: await bounds(page.getByLabel('Start date')), end: await bounds(page.getByLabel('End date')),
        run: await bounds(run), form: await bounds(page.locator('form')),
        guidance: await bounds(page.getByText(/^This checks saved messages only\./)) };
    }
    const initialControls = await controls();
    catalogWait.release();
    await page.waitForFunction(() => !document.querySelector('button[type="submit"]').disabled);
    unchanged(initialControls, await controls(), 'catalog ready');
    await page.getByLabel('Start date').fill('2026-09-19');
    await page.getByLabel('End date').fill('2026-09-18');
    await run.click();
    await page.getByRole('alert').filter({ hasText: /Choose an end date/ }).waitFor();
    unchanged(initialControls, await controls(), 'invalid dates');
    await page.getByLabel('Start date').fill('2026-09-13');
    await page.getByLabel('End date').fill('2026-09-19');
    const answerWait = hold('answer');
    await run.focus(); await run.press('Enter');
    await answerWait.entered;
    unchanged(initialControls, await controls(), 'answer pending');
    assert.equal(await page.getByRole('table').count(), 0);
    answerWait.release();
    await page.getByRole('table').waitFor();
    unchanged(initialControls, await controls(), 'answer ready');
    await page.screenshot({ path: path.join(output, `questions-${width}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    mode = 'error';
    const errorWait = hold('answer');
    await run.click();
    await errorWait.entered;
    assert.equal(await page.getByRole('table').count(), 0);
    unchanged(initialControls, await controls(), 'replacement pending');
    errorWait.release();
    await page.getByRole('alert').filter({ hasText: /could not be prepared/ }).waitFor();
    unchanged(initialControls, await controls(), 'answer error');
    mode = 'stale';
    await run.click();
    await page.getByText(/source messages are no longer available/).waitFor();
    unchanged(initialControls, await controls(), 'longest request guidance');
    mode = 'rows';
    await run.click();
    await page.getByRole('table').waitFor();
    await page.getByRole('button', { name: 'Next page' }).click();
    await page.getByText('Page 2', { exact: true }).waitFor();
    const sourceWait = hold('evidence');
    await page.getByRole('button', { name: /View source 1/ }).click();
    await sourceWait.entered;
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    async function dialogControls(includeHistory = false) {
      const value = { dialog: await bounds(dialog), title: await bounds(dialog.getByRole('heading', { name: 'Source conversation' })),
        close: await bounds(dialog.getByRole('button', { name: 'Close', exact: true })) };
      if (includeHistory) value.history = await bounds(dialog.getByRole('button', { name: /^(Open conversation|Show latest messages)$/ }));
      return value;
    }
    const sourceControls = await dialogControls();
    sourceWait.release();
    await page.getByRole('dialog').getByText('Synthetic <img src=x onerror=alert(1)>', { exact: true }).waitFor();
    unchanged(sourceControls, await dialogControls(), 'source ready');
    assert.equal(await page.getByRole('dialog').locator('img').count(), 0);
    const historyControls = await dialogControls(true);
    const historyWait = hold('history');
    await page.getByRole('button', { name: 'Open conversation', exact: true }).click();
    await historyWait.entered;
    unchanged(historyControls, await dialogControls(true), 'history pending');
    historyWait.release();
    await page.getByText('Synthetic saved conversation', { exact: true }).waitFor();
    unchanged(historyControls, await dialogControls(true), 'history ready');
    const replacementWait = hold('history');
    await page.getByRole('button', { name: 'Show latest messages', exact: true }).click();
    await replacementWait.entered;
    assert.equal(await page.getByRole('list', { name: 'Saved messages' }).count(), 0);
    unchanged(historyControls, await dialogControls(true), 'history replacement pending');
    replacementWait.release();
    await page.getByText('Synthetic saved conversation', { exact: true }).waitFor();
    unchanged(historyControls, await dialogControls(true), 'history replacement ready');
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
      changedSourceCleared: true, noPageOverflow: true, accessibilityViolations: audit.violations.length, geometry });
    await page.close();
  }
  await writeFile(path.join(output, 'report.json'), JSON.stringify({ synthetic: true, results }, null, 2));
  console.log(JSON.stringify(results.map(({ geometry, ...result }) => ({ ...result, geometryChecks: geometry.length }))));
} finally {
  for (const wait of heldResponses) wait.release();
  await browser.close();
}
