import { describe, expect, it } from 'vitest';

import { AnalyticsContractError, parseAnalyticsUpdate } from '../src/analytics';
import { analyticsUpdateFixture, DIGEST } from './analyticsFixture';

describe('strict analytics contract', () => {
  it('parses a valid analytics update document', () => {
    const update = analyticsUpdateFixture();
    expect(parseAnalyticsUpdate(update).projection_generation).toBe(3);
  });

  it.each([
    ['projection digest', (value: any) => { delete value.projection_digest; }],
    ['requested window', (value: any) => { delete value.requested_window; }],
    ['slice provenance', (value: any) => { delete value.slice_provenance.topics; }],
    ['metric provenance', (value: any) => { delete value.metric_provenance.creator_metrics; }],
    ['sentiment provenance', (value: any) => { delete value.sentiment_trend.provenance; }],
  ])('rejects missing %s', (_label, mutate) => {
    const value: any = analyticsUpdateFixture();
    mutate(value);
    expect(() => parseAnalyticsUpdate(value)).toThrow(AnalyticsContractError);
  });

  it.each(['creator_account_id', 'conversation_id', 'participant_id', 'message_id', 'displayName', 'content']) (
    'rejects forbidden raw property %s anywhere in analytics',
    (key) => {
      const value: any = analyticsUpdateFixture();
      value.creator_metrics[key] = 'secret';
      expect(() => parseAnalyticsUpdate(value)).toThrow(/Forbidden analytics property/);
    },
  );

  it('rejects mixed generation identities and unknown contract fields', () => {
    const mixed: any = analyticsUpdateFixture();
    mixed.sentiment_trend.graph_digest = `sha256:${'b'.repeat(64)}`;
    expect(() => parseAnalyticsUpdate(mixed)).toThrow(/active analytics generation/);

    const unknown: any = analyticsUpdateFixture();
    unknown.not_in_contract = DIGEST;
    expect(() => parseAnalyticsUpdate(unknown)).toThrow(AnalyticsContractError);
  });
});
