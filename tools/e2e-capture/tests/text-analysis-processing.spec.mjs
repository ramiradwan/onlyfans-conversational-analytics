import { expect, test } from '@playwright/test';

import { assertJournalOrder, check, dropNextReply, fixture, prepareProfile, processingCommand, readJson, ready, sameMeaning,
  selectProfile, stats, withTextAnalysis } from '../lib/text-analysis-browser.mjs';

test('processing commands preserve durable enrollment and canonical findings', async () => {
  await withTextAnalysis('joint', async (x) => {
    const before = await stats(x.page);
    check(before.canonical.committed_jobs === 0 && before.canonical.client_intents === 0, 'passive_processing');
    await prepareProfile(x);
    await selectProfile(x.page, 'Approved processing profile', 'joint');
    const lostEnable = await dropNextReply(x.page, '/api/v1/nlp/execution');
    await processingCommand(x.page, 'enable', { unknown: true });
    const forwarded = await lostEnable.received; await lostEnable.close();
    check(forwarded.value.receipt.command_key === forwarded.body.command_key && forwarded.value.current.state === 'enabled', 'lost_enable_identity');
    await expect(x.page.getByText('The command outcome is unknown. Check the original retained request before continuing.', { exact: true })).toBeVisible();
    await expect(x.page.getByRole('button', { name: 'Review enablement', exact: true })).toBeDisabled();
    const firstRecovery = x.page.locator('[data-journey-state="textanalysis.processing_recovery"]');
    await expect(firstRecovery).toHaveCount(1);
    await firstRecovery.getByRole('button', { name: 'Check original command', exact: true }).click();
    await expect(x.page.getByText('Processing: enabled.', { exact: true })).toBeVisible();
    await expect.poll(async () => (await stats(x.page)).canonical.committed_jobs,
      { message: 'The licensed worker must seal every frozen source.' }).toBe(fixture.messages.length);
    const computed = await stats(x.page);
    check(computed.engine.analyze_count > 0 && computed.canonical.members === fixture.messages.length && computed.canonical.preview_spent === 0
      && computed.canonical.preview_outputs === 0, 'processing_result');
    const original = x.log.requests.find((item) => item.path === '/api/v1/nlp/execution');
    const firstReceipt = await readJson(x.page, `/api/v1/nlp/execution/commands/${original.body.command_key}`);
    check(firstReceipt.status === 200 && firstReceipt.value.receipt.command_key === original.body.command_key
      && sameMeaning(firstReceipt.value.receipt, forwarded.value.receipt) && sameMeaning(original.body, forwarded.body), 'processing_receipt_missing');
    await ready(x.page, x.log, '/analytics');
    const results = x.page.locator('[data-journey-state="findings.results"]');
    await expect(results).toBeVisible();
    await expect(results.getByRole('table', { name: 'Topics findings', exact: true })).toBeVisible();
    await expect(results.getByRole('table', { name: 'Mentions findings', exact: true })).toBeVisible();
    const topics = results.getByRole('table', { name: 'Topics findings', exact: true });
    const mentions = results.getByRole('table', { name: 'Mentions findings', exact: true });
    const topic = topics.getByRole('row').filter({ hasText: /^topic\.synthetic/ });
    const mention = mentions.getByRole('row').filter({ hasText: /^concept\.synthetic/ });
    await expect(topic.getByRole('cell').nth(1)).toHaveText('2'); await expect(topic.getByRole('cell').nth(2)).toHaveText('2');
    await expect(mention.getByRole('cell').nth(1)).toHaveText('4'); await expect(mention.getByRole('cell').nth(2)).toHaveText('2');
    await expect(topics.getByRole('row').filter({ hasText: /^topic\.secondary/ })).toBeVisible();
    await expect(mentions.getByRole('row').filter({ hasText: /^concept\.secondary/ })).toBeVisible();
    await ready(x.page, x.log);
    await processingCommand(x.page, 'pause');
    const paused = await stats(x.page);
    await ready(x.page, x.log, '/analytics');
    await x.page.getByRole('button', { name: 'Refresh saved findings', exact: true }).click();
    await expect(results).toBeVisible();
    await expect(results.getByText('Processing state when saved: paused; these saved findings remain visible.', { exact: true })).toBeVisible();
    check((await stats(x.page)).engine.analyze_count === paused.engine.analyze_count, 'paused_reanalysis');
    await ready(x.page, x.log);
    await processingCommand(x.page, 'resume');
    await processingCommand(x.page, 'retire');
    const recovery = x.page.locator('[data-journey-state="textanalysis.processing_recovery"]');
    await expect(recovery).toHaveCount(4);
    await recovery.last().getByRole('button', { name: 'Check original command', exact: true }).click();
    const current = x.page.getByRole('region', { name: 'Account text processing', exact: true });
    await expect(current.getByText('Processing: not enrolled.', { exact: true })).toBeVisible();
    const retainedReceipt = await readJson(x.page, `/api/v1/nlp/execution/commands/${original.body.command_key}`);
    check(retainedReceipt.status === 200 && sameMeaning(retainedReceipt.value.receipt, firstReceipt.value.receipt)
      && retainedReceipt.value.current.state === 'retired' && retainedReceipt.value.current.enrollment_id === null
      && retainedReceipt.value.current.mode_version === firstReceipt.value.receipt.mode_version + 3
      && retainedReceipt.value.current.capsule_available === false, 'processing_receipt_rebound');
    await x.log.settle(); assertJournalOrder(x.log);
    const commands = x.log.requests.filter((item) => item.path === '/api/v1/nlp/execution');
    check(commands.map((item) => item.body.action).join(',') === 'enable,pause,resume,retire'
      && new Set(commands.map((item) => item.body.command_key)).size === 4, 'processing_commands');
    const retired = await readJson(x.page, `/api/v1/nlp/execution/commands/${commands.at(-1).body.command_key}`);
    check(retired.status === 200 && retired.value.receipt.state === 'retired' && retired.value.receipt.enrollment_id === null
      && retired.value.receipt.mode_version === retainedReceipt.value.current.mode_version
      && sameMeaning(retired.value.current, retainedReceipt.value.current), 'retirement_receipt');
    const final = await stats(x.page);
    check(final.canonical.committed_jobs === computed.canonical.committed_jobs && final.canonical.members === computed.canonical.members
      && final.engine.analyze_count === paused.engine.analyze_count && final.canonical.client_intents === 4, 'processing_retention');
    check(!x.log.requests.some((item) => item.path.startsWith('/api/v1/nlp/previews')
      || item.path === '/api/v1/nlp/dependencies' && item.body.action !== 'prepare'), 'processing_side_effect');
  });
});
