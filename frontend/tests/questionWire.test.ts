import { expect, it } from 'vitest';

import { parseQuestionResult, parseSourceEvidence, questionPlan } from '../src/analytics/questionContract';
import wire from './fixtures/question-wire.json';
import { answer, NOW, plan } from './questionFixture';

it('accepts source-linked responses from the authenticated SQLite endpoint fixture', () => {
  expect(wire.synthetic).toBe(true);
  const request = questionPlan.parse(wire.request);
  const result = parseQuestionResult(wire.result, request);
  expect(result.page.rows).toHaveLength(2);
  const source = parseSourceEvidence(wire.source, result.page.rows[0].evidence[0]);
  expect(source.reference.source_revision).toBe(result.question.snapshot.source_revision);
  expect(source.location.conversation_id).toBeTruthy();
});

it('requires a complete two-week comparison and a source in each populated week', () => {
  const request = questionPlan.parse({ ...plan, question: 'participant_activity_decline.v1',
    start: new Date(NOW.getTime() - 14 * 86_400_000).toISOString() });
  const result = answer(request);
  result.question.snapshot.source_message_count = 9;
  const row = result.page.rows[0];
  const earlier = { ...row.evidence[0], message_ref: `m1:${'3'.repeat(64)}`, sent_at: request.start };
  const match = { ...row, reason: 'participant_activity_decline', coverage: 'complete', earlier_messages: 6,
    recent_messages: 3, evidence: [row.evidence[0], earlier] };
  const parse = (changes: Record<string, unknown>) => parseQuestionResult({ ...result,
    page: { ...result.page, rows: [{ ...match, ...changes }] } }, request);
  expect(parse({}).page.rows).toHaveLength(1);
  for (const change of [{ earlier_messages: 4 }, { earlier_messages: 10 }, { recent_messages: 4 }, { recent_messages: 0 },
    { coverage: 'partial' }, { evidence: [row.evidence[0]] },
    { evidence: [row.evidence[0], { ...earlier, sent_at: row.evidence[0].sent_at }] }]) {
    expect(() => parse(change)).toThrow();
  }
});
