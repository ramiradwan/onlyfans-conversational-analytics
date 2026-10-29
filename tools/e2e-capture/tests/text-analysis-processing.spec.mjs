import { expect, test } from '@playwright/test';

import { assertJournalOrder, check, dropNextReply, fixture, prepareProfile, processingCommand, readJson, ready, sameMeaning,
  selectProfile, stats, withTextAnalysis } from '../lib/text-analysis-browser.mjs';

test('processing commands preserve durable enrollment and canonical findings', async () => {
  await withTextAnalysis('joint', async (x) => {
    const navigate = (route = '/settings') => ready(x.page, x.log, route, x.conversationCount);
    const before = await stats(x.page);
    check(before.canonical.committed_jobs === 0 && before.canonical.client_intents === 0, 'passive_processing');
    await prepareProfile(x);
    await selectProfile(x.page, 'Approved processing profile', 'joint');
    const lostEnable = await dropNextReply(x.page, '/api/v1/nlp/execution');
    await processingCommand(x.page, 'enable', { unknown: true });
    const forwarded = await lostEnable.received; await lostEnable.close();
    check(forwarded.value.receipt.command_key === forwarded.body.command_key && forwarded.value.current.state === 'enabled', 'lost_enable_identity');
    const processing = x.page.getByRole('region', { name: 'Account text processing', exact: true });
    await expect(processing.getByRole('button', { name: 'Check processing status', exact: true })).toBeEnabled();
    await processing.locator('[data-reserved-region="processing-feedback"]').getByRole('button', { name: 'Show details', exact: true }).click();
    const details = x.page.getByRole('dialog', { name: 'Details', exact: true });
    await expect(details.getByText('The command outcome is unknown. Check the original retained request before continuing.', { exact: true })).toBeVisible();
    await details.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(details).toHaveCount(0);
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
    await navigate('/analytics');
    const results = x.page.locator('[data-journey-state="findings.results"]');
    await expect(results).toBeVisible();
    await expect(results.getByRole('table', { name: 'Topics findings', exact: true })).toBeVisible();
    await expect(results.getByRole('table', { name: 'Mentions findings', exact: true })).toBeVisible();
    const topics = results.getByRole('table', { name: 'Topics findings', exact: true });
    const mentions = results.getByRole('table', { name: 'Mentions findings', exact: true });
    const topic = topics.getByRole('row').filter({ hasText: /^topic\.synthetic/ });
    const mention = mentions.getByRole('row').filter({ hasText: /^concept\.synthetic/ });
    await expect(topic.getByRole('cell').nth(1)).toHaveText('2'); await expect(topic.getByRole('cell').nth(2)).toHaveText(String(x.conversationCount));
    await expect(mention.getByRole('cell').nth(1)).toHaveText('4'); await expect(mention.getByRole('cell').nth(2)).toHaveText('2');
    await expect(topics.getByRole('row').filter({ hasText: /^topic\.secondary/ })).toBeVisible();
    await expect(mentions.getByRole('row').filter({ hasText: /^concept\.secondary/ })).toBeVisible();
    await navigate();
    await processingCommand(x.page, 'pause');
    const paused = await stats(x.page);
    await navigate('/analytics');
    await x.page.getByRole('button', { name: 'Refresh saved findings', exact: true }).click();
    await expect(results).toBeVisible();
    await expect(results.getByText('Processing state when saved: paused; these saved findings remain visible.', { exact: true })).toBeVisible();
    check((await stats(x.page)).engine.analyze_count === paused.engine.analyze_count, 'paused_reanalysis');
    const unchanged = async () => {
      const current = await stats(x.page);
      check(current.engine.analyze_count === paused.engine.analyze_count
        && current.canonical.committed_jobs === paused.canonical.committed_jobs
        && current.canonical.members === paused.canonical.members, 'saved_read_reanalysis');
    };
    const readFromUi = async (pathname, action, accept = () => true) => {
      const [response] = await Promise.all([
        x.page.waitForResponse((entry) => new URL(entry.url()).pathname === pathname && accept(entry.request())), action(),
      ]);
      check(response.status() === 200 && /no-store/.test(response.headers()['cache-control'] ?? ''), 'saved_read_response');
      if (response.request().method() === 'POST') check(Boolean(response.request().headers()['x-csrf-token']), 'saved_read_csrf');
      const value = await response.json(); await unchanged();
      return { value, body: response.request().method() === 'POST' ? response.request().postDataJSON() : null };
    };
    const findings = '/api/v1/insights/findings';
    const selectCategory = async (task, category, button) => {
      const { value } = await readFromUi(findings, () => button.click(),
        (request) => new URL(request.url()).searchParams.get('category_id') === category);
      check(value.processing_mode === 'paused' && value.evidence_page.total === (task === 'message_topics' ? 2 : 4)
        && value.evidence_page.items.every((item) => item.task === task && item.category_id === category)
        && new Set(value.evidence_page.items.map((item) => item.reference.conversation_ref)).size === 1, 'category_source_selection');
      return value;
    };
    const openSource = async (item, button, baseline) => {
      const { value: source, body } = await readFromUi('/api/v1/insights/questions/evidence', () => button.click());
      check(sameMeaning(body, item.reference) && sameMeaning(source.reference, item.reference), 'source_reference');
      const message = fixture.messages.find((entry) => entry.message_id === source.location.message_id);
      check(Boolean(message) && source.location.conversation_id === fixture.messages[0].conversation_id
        && source.text === message.text && source.direction === message.direction, 'source_identity');
      const dialog = x.page.getByRole('dialog', { name: 'Finding source', exact: true });
      await expect(dialog.locator('blockquote')).toHaveText(message.text);
      if (item.reference.span) {
        const points = Array.from(message.text), { start, end } = item.reference.span;
        check(start === 7 && end === 11, 'second_occurrence');
        await expect(dialog.locator('mark')).toHaveText(points.slice(start, end).join(''));
        const surrounding = await dialog.locator('mark').evaluate((mark) => ({ before: mark.previousSibling?.textContent ?? '', after: mark.nextSibling?.textContent ?? '' }));
        check(surrounding.before === points.slice(0, start).join('') && surrounding.after === points.slice(end).join(''), 'exact_unicode_highlight');
        check(source.browser_span.start === points.slice(0, start).join('').length
          && source.browser_span.end === points.slice(0, end).join('').length, 'browser_offsets');
      } else {
        check(source.browser_span === null, 'topic_whole_message');
        await expect(dialog.locator('mark')).toHaveCount(0);
      }
      const { value: context, body: request } = await readFromUi('/api/v1/insights/questions/evidence/context',
        () => dialog.getByRole('button', { name: 'Show surrounding messages', exact: true }).click());
      check(sameMeaning(request, { reference: item.reference, expected_generation: baseline.projection_generation,
        expected_projection_digest: baseline.projection_digest, before: 5, after: 5 })
        && context.schema_version === 'analytics-evidence-context.v1'
        && sameMeaning(context.reference, source.reference) && context.projection_generation === baseline.projection_generation
        && context.projection_digest === baseline.projection_digest && context.items.length === 2
        && !context.has_earlier_stored_items && !context.has_later_stored_items, 'context_binding');
      const anchor = context.items[context.anchor_index];
      check(sameMeaning(anchor.reference, source.reference) && anchor.text === source.text
        && sameMeaning(anchor.browser_span, source.browser_span)
        && context.items.every((entry) => entry.reference.account_ref === source.reference.account_ref
          && entry.reference.conversation_ref === source.reference.conversation_ref)
        && sameMeaning(context.items.map((entry) => entry.text), [...fixture.messages].reverse().map((entry) => entry.text))
        && context.items.every((entry, index) => index === context.anchor_index || entry.browser_span === null && entry.reference.span == null)
        && context.items[0].reference.message_ref !== context.items[1].reference.message_ref, 'context_inventory');
      const contextView = dialog.getByLabel('Surrounding saved messages', { exact: true });
      await expect(contextView).toBeVisible();
      for (const entry of fixture.messages) await expect(contextView.getByText(entry.text, { exact: true })).toBeVisible();
      await expect(contextView.getByText(/^Cited source \/ /)).toHaveCount(1);
      await expect(contextView.locator('mark')).toHaveCount(item.reference.span ? 1 : 0);
      await dialog.getByRole('button', { name: 'Close', exact: true }).click(); await expect(dialog).toHaveCount(0);
      await unchanged();
    };
    const topicSources = await selectCategory('message_topics', 'topic.synthetic', topic.getByRole('button', { name: 'View sources for Topics: topic.synthetic', exact: true }));
    await openSource(topicSources.evidence_page.items[0], results.getByRole('button', { name: /^Topics source 1:/ }), topicSources);
    const mentionSources = await selectCategory('concept_mentions', 'concept.synthetic', mention.getByRole('button', { name: 'View sources for Mentions: concept.synthetic', exact: true }));
    const occurrence = mentionSources.evidence_page.items.findIndex((item) => item.reference.span?.start === 7);
    check(occurrence >= 0, 'second_occurrence_missing');
    await openSource(mentionSources.evidence_page.items[occurrence], results.getByRole('button', { name: new RegExp(`^Mentions source ${occurrence + 1}:`) }), mentionSources);
    const literals = '/api/v1/insights/findings/literals';
    const { value: values } = await readFromUi(literals, () => x.page.getByRole('button', { name: 'Show recurring literal values', exact: true }).click());
    expect(values).toMatchObject({ availability: 'available', unavailable_reason: null });
    expect(values.row_page.items).toContainEqual(expect.objectContaining({ literal_key: 'café', occurrences: 4,
      supporting_messages: 2, supporting_conversations: x.conversationCount }));
    const literal = values.row_page.items.find((item) => item.literal_key === 'café');
    const { value: literalSources } = await readFromUi(literals, () => x.page.getByRole('button', { name: 'View sources for literal café (concept.synthetic)', exact: true }).click(),
      (request) => new URL(request.url()).searchParams.get('node_id') === literal.node_id);
    check(literalSources.node_id === literal.node_id && literalSources.evidence_page.total === 4
      && sameMeaning(literalSources.category_filter, { task: 'concept_mentions', category_id: 'concept.synthetic' }), 'literal_selection');
    const literalOccurrence = literalSources.evidence_page.items.findIndex((item) => sameMeaning(item.reference, mentionSources.evidence_page.items[occurrence].reference));
    check(literalOccurrence >= 0, 'literal_source_identity');
    await openSource(literalSources.evidence_page.items[literalOccurrence], x.page.getByRole('button', { name: new RegExp(`^Literal source ${literalOccurrence + 1}:`) }), literalSources);
    await navigate();
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
  }, { sharedConversation: true });
});
