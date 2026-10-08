import { expect, it } from 'vitest';

import { parseQuestionResult, parseSourceEvidence, questionPlan } from '../src/analytics/questionContract';
import wire from './fixtures/question-wire.json';

it('accepts source-linked responses from the authenticated SQLite endpoint fixture', () => {
  expect(wire.synthetic).toBe(true);
  const request = questionPlan.parse(wire.request);
  const result = parseQuestionResult(wire.result, request);
  expect(result.page.rows).toHaveLength(2);
  const source = parseSourceEvidence(wire.source, result.page.rows[0].evidence[0]);
  expect(source.reference.source_revision).toBe(result.question.snapshot.source_revision);
  expect(source.location.conversation_id).toBeTruthy();
});
