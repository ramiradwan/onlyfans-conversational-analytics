import { expect, test } from '@playwright/test';

import { assertJournalOrder, assertOccurrences, assertSummary, check, confirmPreview, preparePreview, prepareProfile,
  readJson, ready, stats, withTextAnalysis } from '../lib/text-analysis-browser.mjs';

test('joint preview preserves reviewed scope and exact saved evidence', async () => {
  await withTextAnalysis('joint', async (x) => {
    await prepareProfile(x);
    const original = await preparePreview(x);
    const result = await confirmPreview(x, original);
    check(result.scope.inventory_sha256 === original.scope.inventory_sha256 && result.scope.cutoff === original.scope.cutoff
      && result.output.input_inventory_sha256 === original.confirmation.input_inventory_sha256, 'frozen_scope_changed');
    await assertSummary(x.page, result, 'joint'); await assertOccurrences(x.page, result);
    await x.log.settle(); assertJournalOrder(x.log);
    const current = await stats(x.page);
    check(current.canonical.preview_outputs === 1 && current.canonical.preview_spent === 1
      && current.canonical.members === 0 && current.engine.analyze_count > 0, 'shown_only_commit');
    check(!x.log.requests.some((item) => item.path === '/api/v1/nlp/execution'), 'preview_enrollment');
  });
});

test('split preview qualifies partial coverage without inventing totals', async () => {
  await withTextAnalysis('split-partial', async (x) => {
    await prepareProfile(x);
    const original = await preparePreview(x), result = await confirmPreview(x, original);
    await assertSummary(x.page, result, 'split-partial'); await assertOccurrences(x.page, result);
    const topics = result.output.tasks.find((item) => item.task === 'message_topics');
    const mentions = result.output.tasks.find((item) => item.task === 'concept_mentions');
    check(topics.partial > 0 && topics.assessed < topics.eligible && mentions.partial === 0 && mentions.assessed === mentions.eligible, 'split_coverage');
    await expect(x.page.getByLabel('Verified shown preview', { exact: true }).getByText('Uncertain, unsupported and unprocessed messages do not establish absence.')).toHaveCount(2);
    await x.log.settle(); assertJournalOrder(x.log);
    const current = await stats(x.page);
    check(current.canonical.preview_outputs === 1 && current.canonical.preview_spent === 1 && current.canonical.members === 0, 'split_shown_only');
  });
});

test('empty preview retains its outcome without spending admission', async () => {
  await withTextAnalysis('empty', async (x) => {
    await prepareProfile(x); const original = await preparePreview(x);
    await x.page.getByRole('button', { name: 'Confirm original preview', exact: true }).click();
    await expect.poll(async () => (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value?.receipt.state,
      { message: 'The original empty preview must finish.' }).toBe('failed');
    const result = (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value;
    check(result.receipt.terminal_reason === 'empty' && result.receipt.job_id === original.receipt.job_id
      && result.receipt.contribution_id === original.receipt.contribution_id, 'empty_outcome');
    const terminal = await stats(x.page);
    check(terminal.engine.analyze_count > 0 && terminal.canonical.preview_outputs === 0 && terminal.canonical.preview_spent === 0, 'empty_spend');
    await ready(x.page, x.log, '/analytics');
    const recovery = x.page.locator('[data-journey-state="textanalysis.preview_recovery"]').filter({ hasText: 'confirmation' });
    await recovery.getByRole('button', { name: 'Check original preview', exact: true }).click();
    await expect(x.page.getByText('The complete assessment produced no showable output; the preview was not spent.', { exact: true })).toBeVisible();
    await expect(x.page.locator('[data-journey-state="textanalysis.preview_terminal"]')).toBeVisible();
    const after = await stats(x.page);
    check(after.engine.analyze_count === terminal.engine.analyze_count && after.canonical.preview_outputs === 0
      && after.canonical.preview_spent === 0 && after.canonical.client_intents === terminal.canonical.client_intents, 'empty_reexecution');
    const missing = await readJson(x.page, `/api/v1/nlp/previews/results/${original.receipt.contribution_id}/view`);
    check(missing.status === 409 && missing.code === 'text_analysis_preview_refused', 'empty_saved_output');
    await x.log.settle(); assertJournalOrder(x.log);
  });
});
