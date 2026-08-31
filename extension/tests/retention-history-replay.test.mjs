import assert from 'node:assert/strict';
import test from 'node:test';

import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const ACCOUNT = 'retention-history-account';
let idSequence = 0;
const id = () => `61000000-0000-4000-8000-${String(++idSequence).padStart(12, '0')}`;

test('history coordinator emits stale source through the real signer outbox path', async () => {
  const durable = new DurableIngestOutbox({
    storage: new InMemoryIngestionStorage(),
    creatorAccountId: ACCOUNT,
    idFactory: id,
  });
  const state = await durable.initialize();
  const calls = [];
  const signer = {
    async read(request) {
      calls.push(request.operation);
      if (request.operation === 'identity') {
        return {
          operation: 'identity',
          success: true,
          data: { id: 'creator-platform-1' },
        };
      }
      if (request.operation === 'conversations') {
        return {
          operation: 'conversations',
          success: true,
          data: {
            items: [{
              id: 'chat-1',
              platform_user_id: 'participant-1',
              display_name: 'Participant',
              updated_at: '2026-08-30T10:00:00.000Z',
            }],
            continuation: null,
            boundary: 'inventory_end',
          },
        };
      }
      assert.equal(request.operation, 'message-page');
      return {
        operation: 'message-page',
        success: true,
        data: {
          items: [{
            id: 'message-1',
            chat_id: 'chat-1',
            sender_platform_user_id: 'participant-1',
            text: 'stale private text',
            sent_at: '2026-08-30T10:01:00.000Z',
            direction: 'inbound',
          }],
          continuation: null,
          boundary: 'history_start',
        },
      };
    },
  };
  let tick = 0;
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => `2026-08-31T10:${String(tick++).padStart(2, '0')}:00Z`,
    delay: async () => {},
    configuration: () => ({
      creator_account_id: ACCOUNT,
      config_revision: 'retention-config-1',
      history_acquisition: {
        enabled: true,
        consent_revision: 'retention-consent-1',
        authorized_platform_creator_id: 'creator-platform-1',
        recent_window_days: 30,
        page_size: 50,
        pages_per_wake: 10,
        request_interval_ms: 0,
        retry_limit: 2,
      },
    }),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'retention-config-1',
      account_epoch: state.account_epoch,
    }),
  });

  assert.deepEqual(await coordinator.wake(), { status: 'progressed', pages: 2 });
  assert.deepEqual(calls, ['identity', 'conversations', 'message-page']);

  const messageEntry = (await durable.entries()).find(
    (entry) => entry.change?.type === 'message.upsert',
  );
  assert.ok(messageEntry);
  assert.equal(messageEntry.acquisition_origin, 'signer');
  assert.equal(messageEntry.change.message.message_id, 'message-1');
  assert.equal(messageEntry.change.message.text, 'stale private text');
});
