import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { isBrainToBridgeMessage, parseBrainToBridgeMessage } from '../src/protocol';

const FIXTURES = resolve(process.cwd(), '../shared/fixtures/protocol/v2');

function fixture(name: string): Record<string, any> {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8')) as Record<
    string,
    any
  >;
}

describe('Bridge protocol v2', () => {
  it('accepts bounded summaries, metric envelopes, and independent readiness dimensions', () => {
    const parsed = parseBrainToBridgeMessage(fixture('state.snapshot'));
    expect(parsed.protocol_version).toBe('2');
    expect(parsed.type).toBe('state.snapshot');
    if (parsed.type !== 'state.snapshot') return;

    const conversation = parsed.payload.conversations[0];
    expect(conversation.latest_message?.text).toBe('Hello');
    expect(conversation).not.toHaveProperty('messages');

    const metric = parsed.payload.analytics.total_messages;
    expect(metric).toMatchObject({
      value: 1,
      basis: 'synced_subset',
      sample_size: 1,
      projection_revision: 11,
    });
    expect(metric.observed_range).toEqual({
      start: '2026-07-19T09:59:00Z',
      end: '2026-07-19T10:01:00Z',
    });
    expect(metric.complete_range).toBeNull();

    expect(parsed.payload.coverage).toMatchObject({
      status: 'partial',
      as_of: '2026-07-19T10:00:00Z',
      complete_as_of: null,
      reason: 'conversation_evidence_missing',
    });
    expect(parsed.payload.projection).toMatchObject({
      status: 'current',
      canonical_revision: 11,
      projected_revision: 11,
      reason: null,
    });
    expect(parsed.payload.live_freshness).toMatchObject({
      status: 'current',
      pending_count: 0,
      reason: null,
    });
  });

  it('rejects unbounded message arrays and superseded readiness fields', () => {
    const unbounded = fixture('state.snapshot');
    unbounded.payload.conversations[0].messages = [];
    expect(isBrainToBridgeMessage(unbounded)).toBe(false);

    const legacyReadiness = fixture('state.snapshot');
    legacyReadiness.payload.coverage.data_as_of = legacyReadiness.payload.coverage.as_of;
    expect(isBrainToBridgeMessage(legacyReadiness)).toBe(false);
  });

  it('accepts an agent.state describing an account that holds no configuration', () => {
    const noConfiguration = fixture('agent.state');
    noConfiguration.payload.status = 'disconnected';
    noConfiguration.payload.agent_installation_id = null;
    noConfiguration.payload.connection_id = null;
    noConfiguration.payload.required_config_revision = null;
    noConfiguration.payload.applied_config_revision = null;
    noConfiguration.payload.last_heartbeat_at = null;
    noConfiguration.payload.degraded_reason =
      'No Agent configuration is required for this account';

    const parsed = parseBrainToBridgeMessage(noConfiguration);
    expect(parsed.type).toBe('agent.state');
    if (parsed.type !== 'agent.state') return;
    expect(parsed.payload.required_config_revision).toBeNull();
  });

  it('accepts a companion change notice that carries no pairing details', () => {
    const notice = fixture('companion.state');
    const parsed = parseBrainToBridgeMessage(notice);
    expect(parsed.type).toBe('companion.state');
    expect(isBrainToBridgeMessage({ ...notice, payload: { ...notice.payload, comparison_code: '123456' } })).toBe(false);
    expect(isBrainToBridgeMessage({ ...notice, payload: { ...notice.payload, revision: -1 } })).toBe(false);
  });

  it('accepts dimension-only next-revision deltas with the locked coverage vocabulary', () => {
    const snapshot = fixture('state.snapshot');
    const delta = {
      type: 'state.delta',
      protocol_version: '2',
      message_id: '20000000-0000-4000-8000-000000000002',
      payload: {
        creator_account_id: snapshot.payload.creator_account_id,
        view_revision: snapshot.payload.view_revision + 1,
        committed_at: '2026-07-19T10:03:00Z',
        changes: [
          {
            type: 'coverage.replace',
            coverage: {
              ...snapshot.payload.coverage,
              status: 'complete',
              phase: 'complete',
              complete_conversations: snapshot.payload.coverage.discovered_conversations,
              complete_as_of: '2026-07-19T10:03:00Z',
              reason: null,
            },
          },
        ],
      },
    };
    expect(isBrainToBridgeMessage(delta)).toBe(true);
  });
});

it('accepts only the bounded raw disclosure bundle and scoped Full presentation intent', () => {
  const message = fixture('agent.state');
  const intent = { request_id: '77777777-7777-4777-8777-777777777777',
    journey_id: '11111111-1111-4111-8111-111111111111', disclosure_bundle_id: 'a'.repeat(64) };
  message.payload.browser = { capture: 'active', site_access: 'granted', history_permission: 'missing',
    legal_review_required: false, reported_at: '2026-11-18T00:00:00Z', full_intent: intent };
  expect(isBrainToBridgeMessage(message)).toBe(true);
  expect(parseBrainToBridgeMessage(message)).toMatchObject({ payload: { browser: { full_intent: intent } } });
  message.payload.browser.full_intent = { ...intent, disclosure_bundle_id: 'sha256:' + intent.disclosure_bundle_id };
  expect(isBrainToBridgeMessage(message)).toBe(false);
  message.payload.browser.full_intent = { ...intent, creator_account_id: 'other' };
  expect(isBrainToBridgeMessage(message)).toBe(false);
});

it('accepts only the closed revision-bound capture receipt', () => {
  const message = fixture('agent.state');
  const status = { owner_scope: 'a'.repeat(64), consent_epoch: '11111111-1111-4111-8111-111111111111',
    policy_revision: '1', capture: 'paused', receipt: {
      operation_id: '77777777-7777-4777-8777-777777777777', action: 'pause',
      previous_consent_epoch: '22222222-2222-4222-8222-222222222222', outcome: 'applied',
    } };
  message.payload.browser = { capture: 'paused', site_access: 'granted', history_permission: 'missing',
    legal_review_required: false, reported_at: '2026-11-18T00:00:00Z', capture_control: status };
  expect(isBrainToBridgeMessage(message)).toBe(true);
  expect(parseBrainToBridgeMessage(message)).toMatchObject({ payload: { browser: { capture_control: status } } });
  message.payload.browser.capture_control = { ...status, owner_scope: 'foreign' };
  expect(isBrainToBridgeMessage(message)).toBe(false);
  message.payload.browser.capture_control = { ...status, receipt: { ...status.receipt, extra: true } };
  expect(isBrainToBridgeMessage(message)).toBe(false);
});
